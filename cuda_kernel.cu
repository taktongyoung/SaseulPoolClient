// cuda_kernel.cu  (POW kernel: ASCII_HEX + SHA256)
// 변경점:
//  1) nonce 탐색범위: 64hex 중 앞 40글자 0 고정 + 뒤 24글자 변화(= 96-bit)
//     - nonce32_be_from_u96(): nb[0..19]=0, nb[20..23]=hi32, nb[24..31]=lo64
//  2) midstate 캐싱: pow_left prefix full-blocks(64B 단위) 1회 압축 -> shared midstate
//     - nonce마다 tail + nonce + padding 블록(2~3개)만 압축
// 정합성(규칙):
//  SHA256( pow_left(ASCII) + nonce_hex64(ASCII) ) 그대로 유지

#include <stdint.h>

#if __CUDA_ARCH__ >= 350
__device__ __forceinline__ uint32_t ROTR(uint32_t x, int n){ return __funnelshift_r(x, x, n); }
#else
__device__ __forceinline__ uint32_t ROTR(uint32_t x, int n){ return (x >> n) | (x << (32 - n)); }
#endif

__device__ __forceinline__ uint32_t Ch(uint32_t x, uint32_t y, uint32_t z){ return (x & y) ^ (~x & z); }
__device__ __forceinline__ uint32_t Maj(uint32_t x, uint32_t y, uint32_t z){ return (x & y) ^ (x & z) ^ (y & z); }
__device__ __forceinline__ uint32_t BSIG0(uint32_t x){ return ROTR(x,2) ^ ROTR(x,13) ^ ROTR(x,22); }
__device__ __forceinline__ uint32_t BSIG1(uint32_t x){ return ROTR(x,6) ^ ROTR(x,11) ^ ROTR(x,25); }
__device__ __forceinline__ uint32_t SSIG0(uint32_t x){ return ROTR(x,7) ^ ROTR(x,18) ^ (x>>3); }
__device__ __forceinline__ uint32_t SSIG1(uint32_t x){ return ROTR(x,17) ^ ROTR(x,19) ^ (x>>10); }

__constant__ uint32_t Kc[64] = {
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};

__device__ __forceinline__ void sha256_init(uint32_t st[8]){
  st[0]=0x6a09e667; st[1]=0xbb67ae85; st[2]=0x3c6ef372; st[3]=0xa54ff53a;
  st[4]=0x510e527f; st[5]=0x9b05688c; st[6]=0x1f83d9ab; st[7]=0x5be0cd19;
}

__device__ void sha256_compress_block(uint32_t state[8], const uint8_t block[64]){
  uint32_t w[64];
  #pragma unroll
  for(int i=0;i<16;i++){
    w[i] = ((uint32_t)block[i*4+0]<<24) | ((uint32_t)block[i*4+1]<<16) |
           ((uint32_t)block[i*4+2]<<8 ) | ((uint32_t)block[i*4+3]);
  }
  #pragma unroll
  for(int i=16;i<64;i++){
    w[i] = SSIG1(w[i-2]) + w[i-7] + SSIG0(w[i-15]) + w[i-16];
  }

  uint32_t a=state[0], b=state[1], c=state[2], d=state[3], e=state[4], f=state[5], g=state[6], h=state[7];

  #pragma unroll
  for(int i=0;i<64;i++){
    uint32_t T1 = h + BSIG1(e) + Ch(e,f,g) + Kc[i] + w[i];
    uint32_t T2 = BSIG0(a) + Maj(a,b,c);
    h=g; g=f; f=e; e=d+T1; d=c; c=b; b=a; a=T1+T2;
  }
  state[0]+=a; state[1]+=b; state[2]+=c; state[3]+=d; state[4]+=e; state[5]+=f; state[6]+=g; state[7]+=h;
}

__device__ __forceinline__ bool leq_be32(const uint8_t a[32], const uint8_t b[32]){
  #pragma unroll
  for(int i=0;i<32;i++){
    if(a[i] < b[i]) return true;
    if(a[i] > b[i]) return false;
  }
  return true;
}

__device__ __forceinline__ uint8_t nibble_of(char c){
  if(c>='0' && c<='9') return (uint8_t)(c - '0');
  if(c>='a' && c<='f') return (uint8_t)(c - 'a' + 10);
  if(c>='A' && c<='F') return (uint8_t)(c - 'A' + 10);
  return 0;
}

__device__ __forceinline__ void target_hex64_to_bin32_be(const char t64[64], uint8_t out[32]){
  #pragma unroll
  for(int i=0;i<32;i++){
    uint8_t hi=nibble_of(t64[i*2+0]);
    uint8_t lo=nibble_of(t64[i*2+1]);
    out[i]=(uint8_t)((hi<<4)|lo);
  }
}

__device__ __forceinline__ void bytes32_to_hex64_ascii(const uint8_t in[32], char out[64]){
  const char* HEX="0123456789abcdef";
  #pragma unroll
  for(int i=0;i<32;i++){
    uint8_t v=in[i];
    out[i*2+0]=HEX[(v>>4)&0xF];
    out[i*2+1]=HEX[(v   )&0xF];
  }
}

// nonce: 앞 40hex(=20B) 0 고정, 뒤 24hex(=12B) 변화
// nb[0..19]=0, nb[20..23]=hi32(be), nb[24..31]=lo64(be)
__device__ __forceinline__ void nonce32_be_from_u96(uint8_t nb[32], uint32_t hi32, unsigned long long lo64){
  #pragma unroll
  for(int i=0;i<20;i++) nb[i]=0;

  nb[20]=(uint8_t)((hi32>>24)&0xff); nb[21]=(uint8_t)((hi32>>16)&0xff);
  nb[22]=(uint8_t)((hi32>> 8)&0xff); nb[23]=(uint8_t)((hi32    )&0xff);

  nb[24]=(uint8_t)((lo64>>56)&0xff); nb[25]=(uint8_t)((lo64>>48)&0xff);
  nb[26]=(uint8_t)((lo64>>40)&0xff); nb[27]=(uint8_t)((lo64>>32)&0xff);
  nb[28]=(uint8_t)((lo64>>24)&0xff); nb[29]=(uint8_t)((lo64>>16)&0xff);
  nb[30]=(uint8_t)((lo64>> 8)&0xff); nb[31]=(uint8_t)((lo64    )&0xff);
}

__device__ __forceinline__ void sha256_write_out(const uint32_t st[8], uint8_t out[32]){
  #pragma unroll
  for(int i=0;i<8;i++){
    uint32_t v = st[i];
    out[i*4+0]=(uint8_t)((v>>24)&0xff);
    out[i*4+1]=(uint8_t)((v>>16)&0xff);
    out[i*4+2]=(uint8_t)((v>> 8)&0xff);
    out[i*4+3]=(uint8_t)((v    )&0xff);
  }
}

__device__ __forceinline__
void sha256_from_mid_2blocks(const uint32_t mid[8],
                             const uint8_t b0[64],
                             const uint8_t b1[64],
                             uint8_t out[32]){
  uint32_t st[8];
  #pragma unroll
  for(int i=0;i<8;i++) st[i] = mid[i];
  sha256_compress_block(st, b0);
  sha256_compress_block(st, b1);
  sha256_write_out(st, out);
}

__device__ __forceinline__
void sha256_from_mid_3blocks(const uint32_t mid[8],
                             const uint8_t b0[64],
                             const uint8_t b1[64],
                             const uint8_t b2[64],
                             uint8_t out[32]){
  uint32_t st[8];
  #pragma unroll
  for(int i=0;i<8;i++) st[i] = mid[i];
  sha256_compress_block(st, b0);
  sha256_compress_block(st, b1);
  sha256_compress_block(st, b2);
  sha256_write_out(st, out);
}

#define MAX_LEFT_LEN  8192

__global__ void mine_kernel_pow_mode(const char* __restrict__ pow_left, int left_len,
                                     const char* __restrict__ target_ascii64,
                                     unsigned long long start_nonce,
                                     unsigned int nonce_offset,
                                     unsigned int nonce_stride,
                                     char* __restrict__ out_nonce_hex64_ascii,
                                     unsigned char* __restrict__ out_hash32,
                                     int* __restrict__ out_found_flag,
                                     int inner_loop,
                                     int input_mode,
                                     int hash_mode)
{
  // 현재 커널은 ASCII_HEX + SHA256만 사용 (인자 유지용)
  (void)input_mode;
  (void)hash_mode;

  // --- shared: target ---
  __shared__ uint8_t tgt[32];

  // --- shared: midstate cache for pow_left prefix ---
  __shared__ uint32_t sh_mid[8];
  __shared__ uint8_t  sh_tail[64];
  __shared__ int      sh_fullBlocks;
  __shared__ int      sh_r;
  __shared__ uint64_t sh_bits;

  if(threadIdx.x == 0){
    // target
    char tbuf[64];
    #pragma unroll
    for(int i=0;i<64;i++) tbuf[i]=target_ascii64[i];
    target_hex64_to_bin32_be(tbuf, tgt);

    // clamp left_len
    int l = left_len;
    if(l > MAX_LEFT_LEN) l = MAX_LEFT_LEN;
    if(l < 0) l = 0;

    sh_fullBlocks = l / 64;
    sh_r          = l % 64;
    sh_bits       = (uint64_t)(l + 64) * 8ULL; // 메시지 길이 = prefix + nonce(64)

    uint32_t st[8];
    sha256_init(st);

    // prefix full blocks
    for(int b=0;b<sh_fullBlocks;b++){
      uint8_t blk[64];
      #pragma unroll
      for(int j=0;j<64;j++) blk[j] = (uint8_t)pow_left[b*64 + j];
      sha256_compress_block(st, blk);
    }

    #pragma unroll
    for(int i=0;i<8;i++) sh_mid[i] = st[i];

    // tail
    int off = sh_fullBlocks * 64;
    for(int i=0;i<sh_r;i++) sh_tail[i] = (uint8_t)pow_left[off + i];
    for(int i=sh_r;i<64;i++) sh_tail[i] = 0; // 안전
  }
  __syncthreads();

  volatile int* vflag = (volatile int*)out_found_flag;
  if(*vflag) return;

  // gid
  unsigned long long gid = (unsigned long long)blockIdx.x * blockDim.x + threadIdx.x;
  unsigned long long stride = (unsigned long long)(nonce_stride ? nonce_stride : 1u);

  // 96-bit nonce 분해:
  //  - hi32: gid의 하위 32비트(스레드별로 영역 분리)
  //  - lo64: start_nonce + base + it*stride
  uint32_t hi32 = (uint32_t)(gid & 0xffffffffu);

  // lo64 base 인덱스(기존과 동일한 형태 유지)
  unsigned long long base = (gid * (unsigned long long)inner_loop) * stride + (unsigned long long)nonce_offset;
  unsigned long long lo0  = start_nonce + base;

  uint8_t nb[32];
  char    nonce_hex_ascii[64];
  uint8_t h[32];

  const int r = sh_r;

  #pragma unroll 1
  for(int it=0; it<inner_loop; ++it){
    if(*vflag) return;

    unsigned long long lo64 = lo0 + (unsigned long long)it * stride;

    // nonce 96-bit 구성(앞 40hex 0, 뒤 24hex 변화)
    nonce32_be_from_u96(nb, hi32, lo64);
    bytes32_to_hex64_ascii(nb, nonce_hex_ascii);

    // --- build final blocks from (tail[r] + nonce[64]) + padding ---
    if(r == 0){
      uint8_t b0[64], b1[64];

      #pragma unroll
      for(int i=0;i<64;i++) b0[i] = (uint8_t)nonce_hex_ascii[i];

      #pragma unroll
      for(int i=0;i<64;i++) b1[i] = 0;
      b1[0] = 0x80;

      uint64_t bits = sh_bits;
      b1[56]=(uint8_t)((bits>>56)&0xff); b1[57]=(uint8_t)((bits>>48)&0xff);
      b1[58]=(uint8_t)((bits>>40)&0xff); b1[59]=(uint8_t)((bits>>32)&0xff);
      b1[60]=(uint8_t)((bits>>24)&0xff); b1[61]=(uint8_t)((bits>>16)&0xff);
      b1[62]=(uint8_t)((bits>> 8)&0xff); b1[63]=(uint8_t)((bits    )&0xff);

      sha256_from_mid_2blocks(sh_mid, b0, b1, h);
    }
    else if(r <= 55){
      uint8_t b0[64], b1[64];

      #pragma unroll
      for(int i=0;i<64;i++){ b0[i]=0; b1[i]=0; }

      // b0 = tail[r] + nonce[0..63-r]
      #pragma unroll
      for(int i=0;i<64;i++){
        if(i < r) b0[i] = sh_tail[i];
        else     b0[i] = (uint8_t)nonce_hex_ascii[i - r];
      }

      // b1 = nonce[64-r..63] (r bytes) + 0x80 + zeros + length(bits)
      #pragma unroll
      for(int i=0;i<r;i++) b1[i] = (uint8_t)nonce_hex_ascii[(64 - r) + i];
      b1[r] = 0x80;

      uint64_t bits = sh_bits;
      b1[56]=(uint8_t)((bits>>56)&0xff); b1[57]=(uint8_t)((bits>>48)&0xff);
      b1[58]=(uint8_t)((bits>>40)&0xff); b1[59]=(uint8_t)((bits>>32)&0xff);
      b1[60]=(uint8_t)((bits>>24)&0xff); b1[61]=(uint8_t)((bits>>16)&0xff);
      b1[62]=(uint8_t)((bits>> 8)&0xff); b1[63]=(uint8_t)((bits    )&0xff);

      sha256_from_mid_2blocks(sh_mid, b0, b1, h);
    }
    else { // 56..63
      uint8_t b0[64], b1[64], b2[64];

      #pragma unroll
      for(int i=0;i<64;i++){ b0[i]=0; b1[i]=0; b2[i]=0; }

      // b0 = tail[r] + nonce[0..63-r]
      #pragma unroll
      for(int i=0;i<64;i++){
        if(i < r) b0[i] = sh_tail[i];
        else     b0[i] = (uint8_t)nonce_hex_ascii[i - r];
      }

      // b1 = nonce[64-r..63] (r bytes) + 0x80 + zeros (length는 다음 블록)
      #pragma unroll
      for(int i=0;i<r;i++) b1[i] = (uint8_t)nonce_hex_ascii[(64 - r) + i];
      b1[r] = 0x80;

      // b2 = zeros + length(bits)
      uint64_t bits = sh_bits;
      b2[56]=(uint8_t)((bits>>56)&0xff); b2[57]=(uint8_t)((bits>>48)&0xff);
      b2[58]=(uint8_t)((bits>>40)&0xff); b2[59]=(uint8_t)((bits>>32)&0xff);
      b2[60]=(uint8_t)((bits>>24)&0xff); b2[61]=(uint8_t)((bits>>16)&0xff);
      b2[62]=(uint8_t)((bits>> 8)&0xff); b2[63]=(uint8_t)((bits    )&0xff);

      sha256_from_mid_3blocks(sh_mid, b0, b1, b2, h);
    }

    if(leq_be32(h, tgt)){
      if(atomicCAS(out_found_flag, 0, 1) == 0){
        #pragma unroll
        for(int i=0;i<64;i++) out_nonce_hex64_ascii[i] = nonce_hex_ascii[i];
        #pragma unroll
        for(int i=0;i<32;i++) out_hash32[i] = h[i];
      }
      return;
    }
  }
}

