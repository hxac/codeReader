# LOP3 快速反量化：把 int2/int4 位流变成 half2 Tensor Core fragment

## 1. 本讲目标

学完本讲，你应该能够：

1. **推导 `0x6400` 指数魔数**：解释为什么把一个 4-bit nibble 直接塞进某个 FP16 的尾数位，就能得到数值恰好等于 `1024 + n` 的合法 half，全程不需要任何「整数→浮点」转换指令。
2. **读懂 `lop3_dequant` / `lop3_dequant_2bit` 的每一条位操作**：LO/HI 掩码如何一次性抽出两个 half 的 nibble，LOP3 的 LUT 立即数 `(0xf0 & 0xcc) | 0xaa` 如何把「掩码 + 拼指数」压成一条指令，SUB/MUL/ADD 三个 half2 常量如何把比例缩放与零点偏移融合进两条 `__hsub2`/`__hfma2`。
3. **理解 `dequant_kc_vt::apply` 的模板特化与参数索引**：2-bit 与 4-bit 两个特化如何把 LOP3 产出的「格子编号」乘上 scale、加回 zero，产出能直接进 `cute::gemm` 的 FP16 fragment；并对比两种位宽在寄存器产出数量上的差异（8 个 half vs 16 个 half）。
4. **辨别一条过时注释**：源码注释声称融合了 `-8` 对称零点，但当前常量实际输出无符号值 `[0, 15]`——这是与第四单元打包侧仿射量化器（zero = 组内 min）配套的改动。读懂常量而不是盲信注释，是源码精读的基本功。

## 2. 前置知识

### 2.1 FP16（half）的位格式

一个 half 占 16 位：1 位符号 + 5 位指数 + 10 位尾数。规格化数值为：

\[
\text{value} = (-1)^s \times 2^{(E-15)} \times \left(1 + \frac{m}{1024}\right), \quad m = \text{10 位尾数整数}
\]

本讲反复用到两个事实：

- **11 位有效精度**：FP16 能精确表示所有绝对值不超过 2048 的整数（1 位隐含 1 + 10 位尾数 = 11 位）。因此 1024~1264 范围内的整数全是精确值。
- **尾数第 j 位的权重**是 \(2^{E-25+j}\)。这个公式是解锁 `0x6400` 魔数的钥匙（见 4.1.2）。

### 2.2 LOP3：一条指令完成任意三输入按位逻辑函数

`lop3.b32 d, a, b, c, immLut` 是 SM50 起 GPU 提供的指令：对 32 位中的每一位，把 `(a_bit, b_bit, c_bit)` 当作 3 位地址，去 8 位真值表 `immLut` 里查输出位。也就是说，一条 LOP3 可以实现 **任意** a、b、c 的按位布尔组合。本讲用它实现 `(a & b) | c`——传统写法需要 AND + OR 两条指令，LOP3 一条搞定。FasterTransformer 的注释也点明了为什么手写内联 PTX：编译器并不总能自动把 `(x & m) | e` 识别并合并成 LOP3。

### 2.3 Tensor Core fragment 与 half2 寄存器对

SM80 的 `mma.m16n8k16`（FP16 输入）不从共享内存取数，而是要求每个线程用**自己的寄存器**持有操作数碎片（fragment）：A 碎片 4 个 `half2`（8 个 half），B 碎片 2 个 `half2`。数据以 half2（32 位寄存器装两个 half）为单位成对出现，两个 lane 各服务矩阵中相邻的两个位置。本讲的 `FragA`/`FragB` 容器就是为这种「寄存器成对布局」准备的（PTX 对应文档链接见源码注释）。

### 2.4 与前几讲的衔接

- **u4-l3**（打包侧）：量化公式 \(q = \mathrm{clip}(\mathrm{round}((x - z)\cdot s^{-1}),\ 0,\ 2^{b}-1)\)，其中 \(z\) 取组内 min、\(s=\text{range}/(2^b-1)\)，\(q\) 是**无符号**整数。本讲要做的就是把 \(q\) 还原，再算 \(x \approx q\cdot s + z\)。
- **u5-l2**（主循环）：反量化发生在 `gemm_Kchannel`/`gemm_Vtensor` 内部——K/V 打包数据从 smem 拷进寄存器后、MMA 之前的那一小段。本讲就把这一小段放大到指令级。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [csrc/bit_decode/src/include/dequantize.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L1-L477) | 本讲主角：LOP3 位魔法、参数加载、`dequant_kc_vt` 模板，全部在这个头文件里 |
| [csrc/bit_decode/src/include/utils.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L198-L333) | `gemm_Kchannel`/`gemm_Vtensor`/`gemm_Ktensor`：反量化与 MMA 的融合点，说明产物去向 |
| [csrc/bit_decode/src/include/kernel_traits.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L70-L111) | 提供 `pack_num = 16/num_bits`、`num_params = kBlockN_pack/group_size` 等常量 |
| [csrc/bit_decode/src/include/qpack.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L114-L188) | 打包侧量化公式，用来验证本讲反量化的互逆性 |

---

## 4. 核心概念与源码讲解

### 4.1 lop3_dequant：4-bit 反量化的位魔法

#### 4.1.1 概念说明

问题陈述：寄存器里有一个 `int32`，里面紧挨着装着 8 个 4-bit 量化值（两个 uint16 打包槽 × 每槽 4 个 nibble，来自 u2-l1 的 `pack_nums = 16/4 = 4` 打包规则）。MMA 需要 8 个 FP16。最朴素的做法是逐个值执行「移位取出 → 整数转浮点（`cvt`）→ 乘 scale → 加 zero」，约 4~5 条指令一个值。

`lop3_dequant` 的思路完全不同：**不做类型转换，让整数位直接「充当」浮点位**。利用 FP16 编码规则，一个 nibble 在正确放置后本身就构成一个合法的、数值可控的 half；剩下的缩放和偏移用两条 half2 运算吸收。最终 8 个 half 只花约 9 条指令（1 移位 + 4 LOP3 + 4 条 half2 运算），且不占用任何共享内存或查找表。这个技巧源自 FasterTransformer 的 `interleaved_numeric_conversion.h`（源码注释里给了链接），BitDecoding 在其基础上改造了零点处理（见 4.1.2 末尾的「过时注释」辨析）。

它是整个项目「低比特 KV cache 直进 Tensor Core」卖点的最后一环：u2 省下的带宽要在这一步**零成本地**变回可计算的 FP16，反量化若贵，前面的省就白省了。

#### 4.1.2 核心流程

**第一步：推导指数魔数 EX = 0x6400。**

把一个 4-bit 值 \(n\in[0,15]\) 放进某 half 的尾数最低 4 位（bits 3:0），得到的 half 数值是：

\[
2^{(E-15)} \times \left(1 + \frac{n}{1024}\right) = 2^{(E-15)} + n\cdot 2^{(E-25)}
\]

我们希望这个值恰好等于「某个好减的基数 + n」，即让尾数最低位权重 \(2^{(E-25)} = 2^0 = 1\)，解出 \(E=25\)。于是这个 half 为：符号 0、指数字段 25（二进制 `11001`，占 bits 14:10）、尾数 0：

```
0 11001 0000000000  =  0110 0100 0000 0000  =  0x6400
```

而 \(0\text{x}6400 = 2^{10} = 1024\)。于是有恒等式：

\[
\text{bits}(0\text{x}6400 \mid n) \;\equiv\; \text{half}(1024 + n)
\]

由于 \(1024+n \le 1039 < 2048\)，落进 FP16 的整数精确区间，**没有任何舍入误差**。

**第二步：一个 int32 里抽两处 nibble —— LO 与 HI 掩码。**

一个 int32 装两个 half 的位流。`LO = 0x000f000f` 同时选中**低半 16 位的 bits 3:0** 和**高半 16 位的 bits 3:0**（两个打包槽各自第 0 个 nibble）；`HI = 0x00f000f0` 则选中两个半区的 bits 7:4（各自第 1 个 nibble）。由于尾数 bits 4:7 的权重是 16 倍：

\[
\text{bits}(0\text{x}6400 \mid (n \ll 4)) \equiv \text{half}(1024 + 16n)
\]

每条 LOP3 因此一次产出**两个** half（每个 16 位半区各一个）。

**第三步：`(a & b) | c` 压成一条 LOP3。**

「取出 nibble 并拼上指数」= `(q & MASK) | EX`。LOP3 的 LUT 立即数写作 `(0xf0 & 0xcc) | 0xaa`，按位算出来是 `0xC0 | 0xAA = 0xEA = 1110_1010_2`。验证它确实编码了 \(f=(a\&b)\,|\,c\)：输出位 = `lut[(a≪2)|(b≪1)|c]`，枚举 8 种组合，1 的情况恰好是 c=1 或 (a=b=1)。这个立即数的写法本身是个「自文档化」技巧——表达式形状就是它实现的逻辑形状。

**第四步：移动窗口取后两批 nibble。**

`top_i4s = q >> 8` 把两个半区的第 2、3 个 nibble（bits 11:8 / 27:24）移到 bits 3:0 / 19:16，复用同一对 LO/HI 掩码。注意源码特意把它放在四条 LOP3 **之前**发出：后面的 LOP3 依赖 `q`，而 `__hsub2`/`__hfma2` 依赖 LOP3 的结果，提前发射移位能隐藏这条 RAW 依赖（原文注释 "Issue first to hide RAW dependency"）。

**第五步：SUB/MUL/ADD 融合缩放与零点。**

现在的两个中间量是：

- 低路（nibble 在 bits 3:0）：half 值 \(1024 + n\)
- 高路（nibble 在 bits 7:4）：half 值 \(1024 + 16n\)

要把两者都变回 \(n\)，用三个 half2 常量（`0xXXXXXXXX` 高低两个 16 位各放一份，同时服务 half2 的两个 lane）：

| 常量 | 位模式 | half 数值 | 作用 |
| --- | --- | --- | --- |
| `SUB = 0x64006400` | \(2^{10}\) | 1024 | 低路：\((1024+n)-1024 = n\) |
| `MUL = 0x2c002c00` | \(2^{-4}\) | 1/16 | 高路先除以 16 |
| `ADD = 0xd400d400` | \(-2^6\) | −64 | 高路：\((1024+16n)\cdot\frac{1}{16}-64 = 64+n-64 = n\) |

高路为什么不把 nibble 移到 bits 3:0 再减 1024？因为那需要一条额外的可变移位；保持掩码固定、用**一条** `__hfma2` 同时吸收 \(\times 1/16\) 和 \(-64\)，指令数更少。两路的每一步都是精确的：中间值全是 ≤1264 的整数，除以 2 的幂只改指数不碰尾数，减法结果是小整数、可精确表示，浮点舍入为零。

**重要辨析：注释说「融合了 −8 对称零点」，但当前常量并没有。**

源码注释写 *"We want signed int4 outputs, hence we fuse the `-8` symmetric zero point directly into `SUB` and `ADD`"*，且两个常量旁边保留了 `// 0x64086408` 与 `// 0xd480d480` 的备选注释。验算一下备选值：`0x6408` = 1024+8 = 1032，`0xd480` = \(-64\times1.125\) = −72，于是低路 \((1024+n)-1032=n-8\)、高路 \((64+n)-72=n-8\)，产出对称区间的有符号值 \([-8,7]\)——这是 FasterTransformer 为「对称量化」设计的版本。

但当前生效的常量是 `0x6400/0xd400`，产出**无符号** \([0,15]\)。这与 u4-l3 打包侧一致：`qpack.h` 的量化公式把 `zero` 取为组内 min、q 限制在 \([0, 2^b-1]\)，解码端 `x ≈ q·scale + zero` 恰好需要无符号 q。**注释是改造前的遗留，常量才是事实**——交叉验证靠的是打包侧代码，而不是注释文本。

**产出布局。** 8 个 half 按 half2 配对组织，代码注释标明了来源编号：

```text
frag[0] = (槽0的第0个值, 槽1的第0个值)   // 注释 // 0,4
frag[1] = (槽0的第1个值, 槽1的第1个值)   // 注释 // 1,5
frag[2] = (槽0的第2个值, 槽1的第2个值)   // 注释 // 2,6
frag[3] = (槽0的第3个值, 槽1的第3个值)   // 注释 // 3,7
```

即 half2 的两个 lane 分别来自两个相邻 uint16 打包槽的**同序号**值（int32 的 bits 19:16 就是第 4 个 nibble，注释里的「4」即此）。这种配对正是后续 `mma.m16n8k16` B 碎片需要的：两个 lane 对应操作数矩阵中相邻的两个位置（k-channel K 场合下是同一 token 的相邻通道，配对好的 scale half2 两个 lane 也分别服务这两个通道，见 4.3）。

完整流程伪代码：

```text
输入: int32 q（8 个 nibble）
EX=0x6400, LO=0x000f000f, HI=0x00f000f0
t = q >> 8                          // 后半批前移
lo1 = (q & LO) | EX   → half2(1024+n0, 1024+n4)
hi1 = (q & HI) | EX   → half2(1024+16n1, 1024+16n5)
lo2 = (t & LO) | EX   → half2(1024+n2, 1024+n6)
hi2 = (t & HI) | EX   → half2(1024+16n3, 1024+16n7)
frag[0] = lo1 - 1024                // __hsub2
frag[1] = hi1 * (1/16) + (-64)      // __hfma2
frag[2] = lo2 - 1024                // __hsub2
frag[3] = hi2 * (1/16) + (-64)      // __hfma2
输出: FragA = 4 × half2 = 8 个 half，数值 = n0..n7（精确）
```

#### 4.1.3 源码精读

先看基础设施。寄存器容器 `Vec` 与三个碎片类型别名（`FragS` 是为量化 scale 预留的容器，本文件内并未直接使用）：

[dequantize.h:L25-L41](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L25-L41)——`Vec<T,n>` 是纯寄存器数组（`elems[n]`），因为所有下标访问必须是编译期常量，所以调用处一律套 `#pragma unroll`；`FragA = Vec<half2,4>`（8 half）、`FragB = Vec<half2,8>`（16 half），注释里附了 PTX m16n8k16 碎片布局的官方文档链接。

LOP3 的内联 PTX 封装：

[dequantize.h:L46-L54](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L46-L54)——`lop3<lut>(a,b,c)` 用 `"n"(lut)` 约束把真值表烧成立即数；`volatile` 防止编译器把它与相邻位运算做错误的合并/重排。函数头注释解释了为什么要显式手写：编译器并不总能自动识别出可合并的 `(a&b)|c`。

4-bit 反量化主体：

[dequantize.h:L59-L72](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L59-L72)——三个掩码常量与 `top_i4s = q >> 8`（提前发射以隐藏 RAW 依赖），随后四条 `lop3<(0xf0 & 0xcc) | 0xaa>` 分别产出 (0,4)、(1,5)、(2,6)、(3,7) 四对 half2。

[dequantize.h:L74-L77](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L74-L77)——SUB/MUL/ADD 三个常量的定义处。注意：注释宣称融合了 `-8` 对称零点，但 `0x6400/0xd400` 实际输出无符号 \([0,15]\)；行尾的 `0x64086408`、`0xd480d480` 注释才是带 −8 的 FasterTransformer 原版（详见 4.1.2 的辨析）。

[dequantize.h:L79-L97](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L79-L97)——用 `*reinterpret_cast<half2*>(&lo_1)` 把 32 位结果原地视为 half2，两路 `__hsub2`（减 1024）与两路 `__hfma2`（×1/16 再 −64）装配出 `FragA`；行尾注释标注每个 `frag_a[i]` 对应的 nibble 编号。

顺带一提，函数头注释说 "dequantize an int32 value into a full B-fragment of **4 fp16 values**" 也是过时表述——实际产出是 4 个 half2 共 8 个 half。`FragA` 这个名字借用了 MMA 的 A/B 操作数叫法，在这个代码库里它只是「一份寄存器容器」，由 CuTe 的张量视图再映射到真正的 MMA 碎片上。

#### 4.1.4 代码实践

**实践目标**：用一个最小 CUDA 程序验证 `lop3_dequant` 输出的 8 个 half 与期望值**逐位相等**，并亲手确认 `0x6400` 的推导。

**操作步骤**：

1. 在仓库外任选目录新建 `test_lop3.cu`。为了避免拉起 cutlass 依赖（`dequantize.h` 包含了 `cute/tensor.hpp`），直接从项目里抄三小段：`Vec` 模板（L25-31）、`FragA`/`FragB` 别名（L38-39）、`lop3` 与 `lop3_dequant`（L46-98）。下面的示例代码已集齐：

```cpp
// 示例代码（非项目源码）：片段抄自 csrc/bit_decode/src/include/dequantize.h L25-L98
#include <cstdio>
#include <cuda_fp16.h>

template <typename T, int n>
struct Vec { T elems[n]; __device__ T& operator[](int i){ return elems[i]; } };
using FragA = Vec<half2, 4>;

template <int lut>
__device__ inline int lop3(int a, int b, int c) {
  int res;
  asm volatile("lop3.b32 %0, %1, %2, %3, %4;\n"
               : "=r"(res) : "r"(a), "r"(b), "r"(c), "n"(lut));
  return res;
}

__device__ inline FragA lop3_dequant(int q) {
  const int LO = 0x000f000f, HI = 0x00f000f0, EX = 0x64006400;
  const uint32_t top_i4s = q >> 8;
  int lo_1 = lop3<(0xf0 & 0xcc) | 0xaa>(q, LO, EX);
  int hi_1 = lop3<(0xf0 & 0xcc) | 0xaa>(q, HI, EX);
  int lo_2 = lop3<(0xf0 & 0xcc) | 0xaa>(top_i4s, LO, EX);
  int hi_2 = lop3<(0xf0 & 0xcc) | 0xaa>(top_i4s, HI, EX);
  const int SUB = 0x64006400, MUL = 0x2c002c00, ADD = 0xd400d400;
  FragA f;
  f[0] = __hsub2(*reinterpret_cast<half2*>(&lo_1), *reinterpret_cast<const half2*>(&SUB));
  f[1] = __hfma2(*reinterpret_cast<half2*>(&hi_1), *reinterpret_cast<const half2*>(&MUL),
                 *reinterpret_cast<const half2*>(&ADD));
  f[2] = __hsub2(*reinterpret_cast<half2*>(&lo_2), *reinterpret_cast<const half2*>(&SUB));
  f[3] = __hfma2(*reinterpret_cast<half2*>(&hi_2), *reinterpret_cast<const half2*>(&MUL),
                 *reinterpret_cast<const half2*>(&ADD));
  return f;
}

// 把 8 个已知 int4 值按项目打包规则（nibble j = 第 j 个值，小端）塞进一个 int32
__device__ uint32_t pack_int4x8(const int* v) {
  uint32_t q = 0;
  #pragma unroll
  for (int i = 0; i < 8; ++i) q |= (uint32_t)(v[i] & 0xf) << (4 * i);
  return q;
}

__global__ void check(const int* v_in, half* out) {
  uint32_t q = pack_int4x8(v_in);
  FragA f = lop3_dequant((int)q);
  // half2 的两个 lane 分别来自第 i 个与第 i+4 个 nibble（源码注释 // i,i+4）
  #pragma unroll
  for (int i = 0; i < 4; ++i) {
    out[i]     = __low2half(f[i]);
    out[i + 4] = __high2half(f[i]);
  }
}

int main() {
  int v[8] = {0, 1, 7, 8, 9, 14, 15, 3};          // 期望输出恰好就是这 8 个数（无符号！）
  half *d_out, h_out[8]; int *d_v;
  cudaMalloc(&d_v, 32); cudaMalloc(&d_out, 16);
  cudaMemcpy(d_v, v, 32, cudaMemcpyHostToDevice);
  check<<<1,1>>>(d_v, d_out);
  cudaMemcpy(h_out, d_out, 16, cudaMemcpyDeviceToHost);
  int bad = 0;
  for (int i = 0; i < 8; ++i)
    if (__half2float(h_out[i]) != (float)v[i]) { bad++; printf("mismatch at %d\n", i); }
  printf(bad == 0 ? "ALL EXACT\n" : "%d mismatches\n", bad);
  return 0;
}
```

2. 编译运行（需要本地 GPU，本讲义写作环境无 GPU，**结果待本地验证**）：

```bash
nvcc -arch=sm_80 -o test_lop3 test_lop3.cu && ./test_lop3
```

**需要观察的现象**：

- 输出 `ALL EXACT`——8 个 half 与输入 nibble 逐个相等，误差为 0（不是「近似为 0」，是精确相等，见 4.1.2 的精确性论证）。
- 特别注意 `v[3]=8`、`v[6]=15` 也在内：如果常量是注释里宣称的 `0x6408/0xd480`（对称版），输出会是 `-0/…/7` 和 `7`，即 `v-8`；而当前代码应输出原值，直接验证「无符号」这一结论。

**预期结果**：`ALL EXACT`。若把 SUB/ADD 手动换成 `0x64086408`/`0xd480d480` 再跑，输出应变为 `v[i]-8`——这一对照实验就是「过时注释」的铁证。

#### 4.1.5 小练习与答案

**练习 1**：若把 EX 换成 `0x6000`（即指数字段 16），`lo_1` 的 half2 数值变成多少？还能用减法一步还原吗？

**答案**：`0x6000` 的指数字段是 16，值为 \(2^{1}=2\)，尾数第 0 位权重 \(2^{16-25}=2^{-9}=1/512\)，故 nibble n 产出 \(2 + n/512\)——不是整数，减任何常数都无法一步还原。魔数唯一性来自方程 \(2^{E-25}=1\)，解只能是 E=25，即 `0x6400`。

**练习 2**：一条 LOP3 为什么能同时处理两个 half 的 nibble？

**答案**：因为 LO/HI/EX 三个操作数都是「16 位模式水平复制成 32 位」（如 `0x000f000f`）。LOP3 逐位独立运算，低 16 位与高 16 位各自完成 `掩码+拼指数`，一次产出两个 half 的位模式——这正是 half2 的存储方式。

**练习 3**：`top_i4s = q >> 8` 之后，原 int32 的第 7 个 nibble（bits 31:28）出现在 `top_i4s` 的什么位置？由哪条 LOP3 负责抽出？

**答案**：bits 31:28 右移 8 位落到 bits 23:20，属于高半区的 bits 7:4，由 `hi_2 = lop3(top_i4s, HI, EX)` 抽出，经 `__hfma2` 放进 `frag_a[3]` 的第二个 lane（注释 // 3,7 的「7」）。

---

### 4.2 lop3_dequant_2bit：一个 int32 出 16 个 half

#### 4.2.1 概念说明

2-bit 模式下 `pack_num = 16/2 = 8`（[kernel_traits.h:L73](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L73)），一个 uint16 装 8 个值、一个 int32 装 **16** 个值。掩码换成 2-bit 粒度（`0x0003`/`0x0030`），但位魔法的骨架完全不变：偶数序号的值（第 0、2、4、6 个）落在每 8 位段的 bits 1:0 与 bits 5:4，恰好分别对应 LO/HI 掩码位置；奇数序号的值（第 1、3、5、7 个）在 bits 3:2 与 bits 7:6，先整体右移 2 位就回到 LO/HI 可达的位置。

关键差异是**寄存器产出数量翻倍**：同样一个 32 位源寄存器，4-bit 路线产出 `FragA`（4 个 half2 = 8 个 half），2-bit 路线产出 `FragB`（8 个 half2 = 16 个 half）。这有两个直接后果：

1. 指令数变为约 3 移位 + 8 LOP3 + 8 条 half2 运算 ≈ 19 条，但仍低于朴素逐值转换的 ~60 条。
2. 反量化路径的寄存器压力翻倍——这是 u5-l1 观察到的「2-bit 共享内存预算 144/148 KiB 远大于 4-bit 的 77/78 KiB」在寄存器侧的镜像：位宽越低，同样位数的寄存器「装」的数值越多，还原后的 FP16 体积也越大。

#### 4.2.2 核心流程

设 int32 的 16 个 2-bit 值为 \(v_0..v_{15}\)（每 uint16 槽 8 个，小端排列），四个批次：

```text
q           的 LO 位（bits 1:0, 17:16） → v0,  v8     // lo_1_a
(q >> 2)    的 LO 位（bits 3:2, 19:18） → v1,  v9     // lo_1_b
q           的 HI 位（bits 5:4, 21:20） → v2,  v10    // hi_1_a
(q >> 2)    的 HI 位（bits 7:6, 23:22） → v3,  v11    // hi_1_b
top = q>>8  复用以上四组                   → v4..v7, v12..v15
```

数值还原与 4-bit 共用同一套常量——`SUB=0x6400`(1024)、`MUL=0x2c00`(1/16)、`ADD=0xd400`(−64)：

- LO 路：\(0\text{x}6400 \mid v\)（v 占 bits 1:0，数值 \(1024+v\)），减 1024 得 \(v\in[0,3]\)。
- HI 路：\(0\text{x}6400 \mid (v \ll 4)\)（数值 \(1024+16v\)），\(\times 1/16 - 64 = v\)。

中间值 \(1024 \sim 1072\) 全在 FP16 整数精确区间内，依旧**零舍入误差**。产出的 16 个 half 按 half2 配对（注释 `// 0,8`、`// 1,9` … `// 7,15`）：`frag_b[j]` 的两个 lane 分别是两个 uint16 槽的第 j 个值，即 half2 j = \((v_j,\ v_{j+8})\)。

伪代码：

```text
输入: int32 q（16 个 2-bit 值）
t8 = q >> 8;  t2 = q >> 2;  t82 = t8 >> 2
8 条 LOP3: (q,t8 用 LO/HI) × (原值, t2/t82 版本)
lo 批次: x - 1024          // __hsub2 ×4
hi 批次: x/16 - 64         // __hfma2 ×4
输出: FragB = 8 × half2 = 16 个 half = v0..v15（精确）
```

#### 4.2.3 源码精读

[dequantize.h:L104-L107](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L104-L107)——2-bit 专属的 LO/HI 掩码（`0x00030003`/`0x00300030`）与同一个 EX 魔数。

[dequantize.h:L109-L121](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L109-L121)——`top_i4s = q >> 8` 之后，八条 LOP3 分别处理 `(q, q>>2, top_i4s, top_i4s>>2)` × `(LO, HI)` 八种组合，行尾注释标出每条产出的值编号（0,8 / 1,9 / … / 7,15），与 4.2.2 的表格一一对应。

[dequantize.h:L126-L129](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L126-L129)——SUB/MUL/ADD 常量，与 4-bit 版完全相同；此处的行尾注释把数值写明了（`{1024, 1024}`、`{1/16, 1/16}`、`{-64, -64}`），同样保留了 `0x64086408`/`0xd480d480` 的对称版备选。

[dequantize.h:L131-L165](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L131-L165)——`FragB`（8 个 half2）的装配：`lo_*` 系列四条 `__hsub2`，`hi_*` 系列四条 `__hfma2`，下标顺序严格对应 L114-L121 的值编号。

#### 4.2.4 代码实践

**实践目标**：验证 `lop3_dequant_2bit` 的 16 个输出精确等于输入的 16 个 2-bit 值。

**操作步骤**：在 4.1.4 的 `test_lop3.cu` 里追加（示例代码，非项目源码；`lop3_dequant_2bit` 抄自 [dequantize.h:L104-L166](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L104-L166)）：

```cpp
using FragB = Vec<half2, 8>;

__device__ uint32_t pack_int2x16(const int* v) {
  uint32_t q = 0;
  #pragma unroll
  for (int i = 0; i < 16; ++i) q |= (uint32_t)(v[i] & 0x3) << (2 * i);
  return q;
}

__global__ void check2(const int* v_in, half* out) {
  uint32_t q = pack_int2x16(v_in);
  FragB f = lop3_dequant_2bit((int)q);
  #pragma unroll
  for (int i = 0; i < 8; ++i) {          // frag_b[i] = (v_i, v_{i+8})，见源码注释 // i,i+8
    out[i]      = __low2half(f[i]);
    out[i + 8]  = __high2half(f[i]);
  }
}
```

`main` 里把 `v` 换成 16 个取值 0~3 的数组（务必包含 0 和 3），调用 `check2`，同样比对。

**需要观察的现象**：16 个 half 与输入逐个相等。特别验证配对关系：`f[i]` 的 low half 是第 i 个值、high half 是第 i+8 个值——若你把两槽的值故意错开（例如前 8 个全 0、后 8 个全 3），输出应呈 `0×8, 3×8` 的两段分布。

**预期结果**：全部精确相等（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：2-bit 的 HI 掩码 `0x00300030` 选中的 bits 5:4，其尾数权重是多少？为什么 MUL 仍可用 1/16？

**答案**：bits 5:4 即尾数第 4~5 位，权重 \(16\sim 32\) 倍于最低位，放置 2-bit 值 v 后 half 数值为 \(1024+16v\)。乘 1/16 得 \(64+v\)，减 64 还原——与 4-bit 完全同构，因此 MUL/ADD 无需改动。

**练习 2**：同样是「一个 int32 还原」，2-bit 与 4-bit 的寄存器产出比是多少？这对 kernel 资源预算意味着什么？

**答案**：8 half vs 16 half，2-bit 是 4-bit 的两倍。反量化路径的寄存器占用与 `FragB` 相关的中间缓冲都随之翻倍；这与 u5-l1 中 2-bit 需要更大的共享内存/更受限的架构支持（仅 sm_80/90）是同一件事的两面：压缩越狠，还原后的「膨胀」越大。

**练习 3**：`lop3_dequant_2bit` 里 `q >> 2` 与 `q >> 8` 各解决什么问题？

**答案**：`q >> 2` 把奇数序号值（bits 3:2、7:6）搬到 LO/HI 掩码可达的 bits 1:0、5:4；`q >> 8` 把第二个 uint16 槽的 8 个值整体搬到第一个槽的位段，使同一对掩码能覆盖 16 个值。两者组合后 4 种输入（q、q>>2、q>>8、(q>>8)>>2）× 2 种掩码 = 8 条 LOP3 恰好覆盖 16 个值。

---

### 4.3 dequant_kc_vt::apply：接上 scale/zero 并对齐 fragment

#### 4.3.1 概念说明

`lop3_dequant` 只还原出「格子编号」\(q\in[0,2^b-1]\)，还不是注意力里的 K/V 值。完整反量化是 u4-l3 打包公式的逆运算：

\[
x \;\approx\; q \cdot s + z, \qquad s=\frac{\max-\min}{2^b-1},\;\; z=\min
\]

这一步由模板结构体 `dequant_kc_vt<num_bits, ...>` 完成，按 `num_bits` 提供 2/4 两个特化；外层包装函数 `dequant_Kchannel_Vtensor` 负责 dispatch。它解决三件事：

1. **向量化视角**：用 `cute::recast` 把 source 从 uint16 视图重铸成 uint32（`lop3_dequant` 的输入粒度）、把 target 从 half 重铸成 half2（`__hfma2` 的运算粒度）、把 scales/zeros 重铸成 half2——后者的两个 lane 恰好分别服务 half2 输出的两个 lane。
2. **逐组取参**：group_size 决定多少个连续 packed 值共享一组 (s, z)。`load_params_Kchannel` / `load_params_Vtensor` 先把 smem 里的参数搬到寄存器（u5-l2 主循环阶段的一环），`apply` 再按输出列号换算它属于哪个量化组。
3. **产物即 MMA 操作数**：调用点在 `utils.h` 的 `gemm_Kchannel`/`gemm_Vtensor` 里，反量化紧跟在 smem→寄存器拷贝之后、`cute::gemm` 之前——LOP3 产物不落共享内存，直接被下一行 MMA 消费。

另有 `dequantize_Ktensor`（k-tensor 模式的自由函数版本）：与 u4-l3 的 `quant_Ktensor` 一样属于**当前被禁用的路线**（dispatch 分支被注释），本讲只作对照阅读。

#### 4.3.2 核心流程

4-bit 特化（`num_bits=4`，`num_params = kBlockN_pack/group_size`，见 [kernel_traits.h:L111](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L111)）：

```text
局部常量: pack_num = 4 / num_params        // g=128 → 4；g=32 → 1
recast: source → uint32, target/scales/zeros → half2
for i in size<0>(source_vec):            // 每线程持有的 int32 行（源码注释 "// 2"）
  for p in size<1>(source_vec):          // 第二个 fragment 轴上的 int32
    src_val = lop3_dequant(source_vec(i,p))     // 4 个 half2 = 8 个 half = q 值
    col_offset = p * 4                          // p 轴在 target 上占 4 列
    for j in {0,1,2,3}:
      idx = i + j/pack_num * channel_stride + p * scales_stride
      target_vec(i, col_offset+j) = __hfma2(src_val[j], scales_vec(idx), zeros_vec(idx))
```

索引项的含义：

- `j / pack_num`：输出第 j 列属于哪个量化组。g=128 时 `pack_num=4`，`j/4=0`——一个 uint16 里的 4 个连续 token 必然同属一个 128-group，四个输出共享同一组 (s,z)；g=32 时 `pack_num=1`，`j/1=j`——逐列各取各的参数列。
- `channel_stride = size<0>(source_vec)`：跨到下一个参数组在 per-thread 寄存器布局里的步长。
- `p * scales_stride`：p 轴（第二个 fragment 轴）对应的参数行偏移。

2-bit 特化结构相同，两点差异：调用 `lop3_dequant_2bit`（一次产出 8 个 half2），且因 2-bit 下参数按 half2 两两成组，`num_params_ = num_params/2`（源码标注 `TODO: only for g128`——当前仅 group_size=128 的 2-bit 路线经过验证）。

**half2 的 lane 语义**：`__hfma2(a, b, c)` 逐 lane 计算 `a_lane·b_lane + c_lane`。`src_val[j]` 的两个 lane 来自两个相邻 uint16 槽（4.1.2 的配对），`scales_vec(idx)` 恰好也是把相邻两个通道的 scale 打包成 half2——两个 lane 各服务各的通道，一次指令完成两个输出元素的仿射还原。这就是 `dequantize.h` 中 scale/zeros 一律以 half2 视图出现的原因（smem 中参数也按 `array_aligned<__half2, ...>` 存放，见 u5-l1 的 SharedStorage）。

调用侧的融合（以 K 为例）：

```text
gemm_Kchannel(acc, tCrA, tCrB_i4, tCrB_dequant, scales, zeros, sK_params, ...):
  load_params_Kchannel(scales, zeros, sK_params, tidx, i, num_params)   // 一次性装载本 tile 参数
  copy(smem → tCrB_i4 第一批)                // 打包数据进寄存器
  dequant_Kchannel_Vtensor<4>(tCrB_i4[..0], tCrB_dequant[..0], scales, zeros)
  循环 k 块:
    预取下一批 copy + dequant                 // 软件流水
    cute::gemm(tiled_mma, tCrA[..i], tCrB_dequant[..i], acc)   // MMA 吃的是 FP16 寄存器
```

即 u5-l2 五阶段里的「寄存器反量化」阶段，展开后就是本讲的两个 `lop3_*` 函数加一层 `__hfma2`。

#### 4.3.3 源码精读

参数装载（供 `apply` 消费的寄存器侧 scales/zeros 从哪来）：

[dequantize.h:L174-L194](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L174-L194)——`load_params_Kchannel`：从 smem 参数张量 `params(组号, 通道号)` 取值，列号由 `8*i + 4*(j/num_params) + tidx%4` 拼出（`tidx%4` 让同 quad 的 4 个线程各管 4 个相邻通道，与 half2 配对对齐）；scale 区在列 0 起、zero 区在列 64 起（对应 u4-l3 写入端的两个半区）。作者自己留了一行注释 *"seems no one can know why is this offset ..."*——索引推导的难度可见一斑，完整推导留作本讲练习。

[dequantize.h:L221-L239](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L221-L239)——`load_params_Vtensor`：V 的 tensor 布局参数按 `128*(i/8) + 8*(i%8) + 4*(j/num_params_2) + tidx%4` 取，2-bit 时 `num_params_2 = num_params/2`，与 4.3.2 所述的 half2 成组一致。

4-bit 特化主体：

[dequantize.h:L325-L347](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L325-L347)——`dequant_kc_vt<4,...>::apply` 的签名与 `pack_num = 4/num_params`、三个 `recast`（source→uint32、target/scales/zeros→half2）与两个步长（`channel_stride`、`scales_stride`）的定义。

[dequantize.h:L352-L375](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L352-L375)——双重循环（行尾注释 `// 2`、`// 1` 标出该配置下的循环次数）：L360 调 `lop3_dequant`，L362 计算 `col_offset = p*num_bits`，L370 标注 `TODO: hard code for now 2`（`params_crd=i` 只在 i 维长度为 2 时成立），L372-L375 四条 `__hfma2` 按 `i + j/pack_num*channel_stride + p*scales_stride` 取参数——正是 4.3.2 伪代码的落地。

2-bit 特化与 dispatch：

[dequantize.h:L253-L323](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L253-L323)——`dequant_kc_vt<2,...>::apply`：L269-L270 定义 `num_params_ = num_params/2`（`TODO: only for g128`）与 `pack_num = 4/num_params_`；L287 调 `lop3_dequant_2bit`；L291 的单条 `__hfma2` 在 `j` 循环里展开 8 次（L294-L310 是等价的手工展开注释版）。

[dequantize.h:L386-L401](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L386-L401)——`dequant_Kchannel_Vtensor<num_bits>(...)`：唯一作用是把调用转发给 `dequant_kc_vt<num_bits,...>::apply`，是 u5-l2 主循环与 utils.h 调用的统一入口。

被禁用的 k-tensor 对照路线：

[dequantize.h:L403-L475](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L403-L475)——`dequantize_Ktensor`：L422 硬编码 `kNumBits = 4`；L441 同样调 `lop3_dequant`（位魔法是共用的！）；区别只在参数形状——L451-L459 用 `__half2half2` 把**标量** scale/zero 广播成 half2（k-tensor 模式下一个组共享同一参数，不存在 lane 各异的情况），L461-L464 完成仿乘。当前 dispatch（decode_api.cpp）未启用该路线，仅残留于 residual kernel 的注释代码中。

MMA 融合点：

[utils.h:L249-L287](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L249-L287)——`gemm_Kchannel`：L265-L268 预装载全部 K 参数；L270-L274 第一批 smem→寄存器拷贝后立刻反量化；L276-L286 软件流水（预取 i+1 批 + 反量化），L285 `cute::gemm(tiled_mma, tCrA, tCrB_dequant, acc)`——MMA 读的是 `tCrB_dequant`，即本讲产物的最终去向。

[utils.h:L198-L233](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L198-L233)——`gemm_Vtensor`：V 侧同构实现，L216-L217/L226-L227 显示它按 k 块逐次调 `load_params_Vtensor + dequant_Kchannel_Vtensor`（V 参数随块变化，不能一次装完），L230 同样以 `tCrB_dequant` 喂 MMA。

#### 4.3.4 代码实践

**实践目标**：书面证明「LOP3 反量化 + `__hfma2`」与 u4-l3 打包公式严格互逆，并核对两侧索引的镜像关系。

**操作步骤**：

1. 写下打包侧（[qpack.h:L114-L188](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L114-L188)）：

\[
q = \operatorname{clip}\big(\operatorname{round}((x - z)\cdot s^{-1}),\ 0,\ 2^b-1\big),\qquad s=\frac{\max-\min}{2^b-1},\quad z=\min
\]

（L115 `max_val`、L137-L142 `scale_inv`/`zero=min`、L181-L188 clip+round。）

2. 写下解码侧（本讲）：

\[
\hat{x} = \mathrm{lop3}(q)\cdot s + z
\]

3. 推导误差界：设 \(r = \mathrm{round}((x-z)s^{-1})\) 未被 clip 截断，则 \(q = r\)，\(\hat{x} = q\cdot s + z\)，于是

\[
|\hat{x} - x| = |(r - (x-z)s^{-1})|\cdot s \le \tfrac{1}{2}\cdot s
\]

即**还原误差只来自打包侧的舍入，上界 scale/2**；LOP3 与 `__hfma2` 本身零误差（4.1/4.2 已证每一步精确，`__hfma2` 对可表示的输入只做一次舍入，此处乘加结果仍为小数值、误差可忽略）。这解释了 u1-l4 观察到的「MAE 非零且 2-bit 明显大于 4-bit」：\(s\) 随位宽变小而变大。
4. 核对镜像索引：对比 [qpack.h:L161-L178](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/qpack.h#L161-L178) 的 `i + (jj)/pack_num * channel_stride` 与 [dequantize.h:L372-L375](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L372-L375) 的 `params_crd + j/pack_num*channel_stride + p*scales_stride`——两侧用**同名**的 `pack_num`/`channel_stride` 做同构的分组选择，写读路径互为镜像。

**需要观察的现象 / 预期结果**：这是一道推导型实践，无运行步骤；预期产出是一页包含上述两条公式与误差界 \(s/2\) 的证明。若想数值验证，可在 Python 端用 numpy 对随机矩阵做 `pack → lop3 逻辑的 numpy 等价实现 → dequant` 往返（uint16 打包用按位或模拟），统计最大误差并确认 ≤ \(s/2\)（numpy 模拟的写法可参考 u4-l3 综合实践，此处不重复）。

#### 4.3.5 小练习与答案

**练习 1**：`dequant_kc_vt<4>` 中 `pack_num = 4/num_params`，group_size=128 与 32 时分别等于多少？各对应什么样的参数共享方式？

**答案**：`num_params = 128/group_size`（kBlockN_pack=128）：g=128 → num_params=1 → pack_num=4，一个 uint16 的 4 个 token 共享一组 (s,z)，`j/4` 恒为 0；g=32 → num_params=4 → pack_num=1，`j/1=j`，输出各列从不同参数列取值。`pack_num` 的语义是「每多少个输出列换一组参数」。

**练习 2**：为什么 `dequantize_Ktensor` 用 `__half2half2` 广播标量参数，而 `dequant_kc_vt` 直接用 half2 视图的 scales？

**答案**：k-tensor 模式下一个量化组的所有元素共享同一个 scale/zero（组沿通道切、参数与序列无关），两个 lane 参数相同，用 `__half2half2` 把标量复制成 half2 即可；k-channel 模式下 half2 的两个 lane 对应两个相邻通道，各自有独立的 scale（每通道一组参数），必须从按 half2 打包存储的参数区直接取——两种取法对应两种量化模式的本质差异。

**练习 3**：`load_params_Kchannel` 中 zero 为何从列 64 起取？这个 64 从哪来？

**答案**：u4-l3 的落盘约定把参数 smem 区分成两个半区：前 64 列（按 `tile_paramsk_j` 分块的 scale 面）与后 64 列（zero 面），读写两侧（[qpack 写入] 与本讲 [读取]）按同一偏移镜像。`SmemLayoutKParams_channel`（[kernel_traits.h:L184-L187](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L184-L187)）的形状 `tile_paramsk_j × kHeadDim` 正是这两个 64 列面的形状来源（128 通道对半分开）。

---

## 5. 综合实践

**任务：把 4.1.4 与 4.2.4 的两个最小验证合并成一个完整的 `test_lop3_all.cu`，并附一页 `EX_DERIVATION.md` 推导。**

具体要求：

1. **程序部分**（示例代码框架见 4.1.4/4.2.4，均为非项目源码）：
   - `check` 内核：8 个已知 int4 → `pack_int4x8` → `lop3_dequant` → 比对，期望 `ALL EXACT`；
   - `check2` 内核：16 个已知 int2 → `pack_int2x16` → `lop3_dequant_2bit` → 比对，期望 `ALL EXACT`；
   - 对照实验：把 SUB/ADD 换成 `0x64086408`/`0xd480d480` 重跑 `check`，记录输出变为 `v[i]-8`，用实际运行结果坐实「源码注释过时、当前常量为无符号」的判断；
   - 编译命令：`nvcc -arch=sm_80 -o test_lop3_all test_lop3_all.cu`（需本地 sm_80 及以上 GPU；本讲义写作环境无 GPU，**全部结果待本地验证**）。
2. **推导部分**（写入 `EX_DERIVATION.md`，可不放入仓库）：
   - 从 FP16 公式出发解方程 \(2^{E-25}=1\)，得出 E=25、位模式 `0110 0100 0000 0000`、即 `0x6400`；
   - 推导 LO 路 `1024+n`、HI 路 `1024+16n`，以及 SUB/MUL/ADD 三常量的还原恒等式；
   - 说明每一步为何精确（整数 ≤1264、除以 2 的幂只改指数、结果小整数可表示）；
   - 最后一段解释：若量化器改为对称模式（zero 固定为 \(-(2^{b-1})\cdot s\)，如 FasterTransformer），常量应如何改回 `0x6408/0xd480`，以及为什么本项目不能那么改（u4-l3：zero 取组内 min 的仿射量化）。
3. **延伸观察（可选）**：在 `evaluation/test.py` 中把 `num_bits` 从 4 改为 2 重跑（参考 u1-l4 的实践），结合本讲「2-bit 寄存器产出翻倍、误差上界 s 更大」两点，解释观察到的误差差异与性能差异。

预期产出：一个通过的自验证程序 + 一页可复查的数学推导。完成后，你对「BitDecoding 为什么能把反量化做进 Tensor Core 流水线」应该有了指令级的答案。

## 6. 本讲小结

- **`0x6400` 魔数**：指数字段 25 使 FP16 尾数最低位权重恰为 1，nibble 放 bits 3:0 得精确值 `1024+n`、放 bits 7:4 得 `1024+16n`——整数位直接「充当」浮点位，无需任何 `cvt` 转换指令。
- **LOP3 融合**：`(q & MASK) | EX` 用 LUT `0xEA`（源码写作 `(0xf0&0xcc)|0xaa`）压成单指令，且因掩码是 16 位模式复制，一条 LOP3 同时服务 half2 的两个 lane；4-bit 约 9 条指令还原 8 个 half，2-bit 约 19 条还原 16 个 half。
- **SUB/MUL/ADD 三常量**：`1024`、`1/16`、`−64` 把缩放与零点偏移吸收进 `__hsub2`/`__hfma2`；当前常量输出**无符号** [0,15]，与 u4-l3 「zero=组内 min」的仿射量化器互逆，源码中「−8 对称零点」注释是 FasterTransformer 遗留、已过时（`0x6408/0xd480` 才是对称版）。
- **`dequant_kc_vt::apply`**：2/4-bit 双特化，`recast` 到 uint32/half2 视图后调 `lop3_dequant(_2bit)`，再按 `j/pack_num`（pack_num=4/num_params）选组、用 `__hfma2(q, scale, zero)` 完成 \(x\approx q\cdot s+z\)；还原误差只来自打包侧舍入，上界 \(s/2\)。
- **寄存器产出差异**：同一 int32，4-bit 出 FragA（4×half2），2-bit 出 FragB（8×half2）——位宽减半、还原后寄存器占用翻倍，与 2-bit 更大的共享内存预算互为因果。
- **工程位置**：反量化内联在 `gemm_Kchannel`/`gemm_Vtensor` 的软件流水里，产物 `tCrB_dequant` 直接喂 `cute::gemm`，全程不落共享内存——这是「低比特 KV 直进 Tensor Core」的最后一环。

## 7. 下一步学习建议

- **u5-l4（残余 kernel）**：`compute_attn_1rowblock_residualkv` 在 kernel 内**再量化**残余区时复用的正是 u4-l3 打包原语与本讲的逆视角——读它时留意 FP16 残余路径如何绕开 LOP3（不需要反量化），以及攒满触发时 `qpack` 的调用位置。
- **u5-l5（combine kernel）**：反量化后的部分结果如何按 LSE 权重合并，把本讲的「数值正确性」延伸到「跨 split 数值一致性」。
- **源码延伸阅读**：FasterTransformer 的 `interleaved_numeric_conversion.h`（源码注释给出的出处）对比 BitDecoding 的改造点；以及 [dequantize.h:L189](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/dequantize.h#L189) 作者自留的 "seems no one can know why is this offset" 一行——尝试借助 `DEBUG2` 打印（[flash_fwd_kernel.h:L1593-L1609](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1593-L1609)）补全 `load_params_*` 索引的完整推导，是检验你是否真正吃透本讲的高阶练习。
