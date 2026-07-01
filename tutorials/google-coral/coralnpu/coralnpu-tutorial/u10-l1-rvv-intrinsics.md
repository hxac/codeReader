# RVV intrinsics 向量编程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 RISC-V 向量（RVV）intrinsics 的编程模型：`vsetvli` 如何决定「一次处理多少个元素」，以及软件层 stripmining 循环怎么写。
- 读懂 CoralNPU 自带的向量范例 `rvv_add_intrinsic.cc`，理解 `vle`（加载）、`vwadd`（加宽加法）、`vse`（存储）这一条完整的数据通路。
- 掌握 `sw/opt/rvv_opt.h` 里 `Memcpy`/`Memset` 用 `vsetvl` 动态求 `vl` 的「正经写法」，并理解它和范例里写死 `vl=32` 的区别。
- 学会用 `coralnpu_test_utils/rvv_type_util.py` 这个 Python 黄金模型去**确认**一个向量类型能装下多少元素（VLMAX），并了解 `rvv_cpp_util_header_generator.py` 如何自动生成一套类型安全的 C++ 向量算子封装头。

本讲属于**软件层**讲义：我们暂时不碰 RTL，只关心「怎样用 C/C++ intrinsics 写出能被 CoralNPU 向量后端高效执行的程序」。硬件侧的向量译码、ROB、MAC 引擎已在 u7 系列讲过；本讲是把视角拉回到「程序员怎么驱动这些硬件」。

## 2. 前置知识

在开始前，请确保你理解以下概念（前面讲义已建立）：

- **RVV（RISC-V Vector Extension）**：RISC-V 的向量扩展。它和传统 SIMD（如 x86 SSE/AVX）最大的不同是「**寄存器宽度对程序员透明**」——你写的代码不写死「一次算 4 个 int」，而是写「按当前向量长度尽量多算」，同一段代码能跑在不同 VLEN 的机器上。
- **VLEN**：一个向量寄存器的位宽。CoralNPU 生产配置里 **VLEN = 128 位**（见 [rvv_backend_define.svh](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L129-L147) 的 `` `VLEN `` 与各 BUILD 里统一的 `-DVLEN_128`）。VLEN 是可参数化的（128/256/512/1024），但默认 128。
- **SEW（Selected Element Width）**：当前每个元素的位宽，CoralNPU 向量核支持 8/16/32 位（见 [overview.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/overview.md#L40)）。
- **LMUL**：寄存器分组，把多个物理向量寄存器「粘」成一个逻辑向量寄存器组，从而在一条指令里处理更多元素。记法 `m1/m2/m4/m8` 表示用 1/2/4/8 个寄存器。
- **VLMAX 与 VL**：给定 SEW 和 LMUL，一组向量寄存器最多能装下的元素数叫 **VLMAX**；一次实际处理的元素数叫 **VL**，由 `vsetvli` 指令据应用需求算出。三者关系：

\[
\text{VLMAX} = \frac{\text{VLEN} \times \text{LMUL}}{\text{SEW}}
\]

例如 VLEN=128、SEW=8、LMUL=4（即 `int8m4`）：VLMAX = 128×4/8 = **64** 个 int8。

- **stripmining（剥矿）**：当数组长度超过 VLMAX 时，用循环每次「啃」走 VL 个元素，直到啃完整条数组。注意有**两层** stripmining：本讲讲的是 **C 程序员手写的软件层 stripmining**；u7-l6 讲过硬件层还有「一次派发→四次发射」的硬件 stripmining。两者不要混淆。
- 前置讲义：你需要会编译一个 CoralNPU 裸机程序（[u2-l2](u2-l2-write-compile-program.md)）并对向量后端有宏观认识（[u7-l6](u7-l6-stripmining-encoding.md)）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [examples/rvv_add_intrinsic.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/examples/rvv_add_intrinsic.cc) | 入门范例：两个 int8 数组「加宽相加」成 int16 输出，演示 `vle8`/`vwadd`/`vse16`。 |
| [sw/opt/rvv_opt.h](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/rvv_opt.h) | 用 intrinsics 实现的 `Memcpy`/`Memset`，展示「`vsetvl` 动态求 vl + while 循环」的正经写法。 |
| [coralnpu_test_utils/rvv_type_util.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_type_util.py) | Python 黄金模型：把 dtype→SEW、SEW+LMUL→VLMAX、以及 `vtype` 寄存器编码全部列成查表，用于确认向量类型容量与硬件 vtype 编码。 |
| [coralnpu_test_utils/rvv_cpp_util_header_generator.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_cpp_util_header_generator.py) | 脚本：自动生成一份类型安全的 C++ 头 `rvv_cpp_util.h`，把上百个 `__riscv_*` intrinsic 包成模板函数，供 cocotb 测试复用。 |
| [examples/BUILD.bazel](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/examples/BUILD.bazel#L24-L27) | 用 `coralnpu_v2_binary` 规则把范例编译成可在仿真器加载的 `.elf`。 |

## 4. 核心概念与源码讲解

### 4.1 RVV 编程模型与 vsetvli

#### 4.1.1 概念说明

写 RVV 程序的核心思路是「**问硬件：按我指定的元素宽度和分组，你这一拍最多能算几个？然后我就给你那么多个。**」

这条「问」的指令就是 `vsetvli`。它做两件事：

1. 把「SEW / LMUL / 尾元素处理策略」写进 `vtype` CSR，设定好向量寄存器组的解释方式。
2. 计算 `vl = min(AVL, VLMAX)`，其中 AVL（Application Vector Length）是你传入的「我还剩多少元素要算」。`vl` 被写进 `vl` CSR，后续所有向量指令只处理前 `vl` 个元素。

程序员要做的只有一件循环：**只要还有剩余元素，就 `vsetvli` 问一次容量，处理 `vl` 个，指针前移 `vl`，剩余减 `vl`，直到处理完。** 这就是软件层 stripmining。

#### 4.1.2 核心流程

一次典型的向量 memcpy：

```text
n = 待拷贝字节数
while n > 0:
    vl = vsetvli(avl=n, SEW=8, LMUL=m8)   # 问硬件：8位元素、m8分组，这次能给几个
    v  = vle8(src, vl)                     # 加载 vl 个字节
    vse8(dst, v, vl)                       # 存储 vl 个字节
    src += vl; dst += vl; n -= vl          # 推进指针、缩小剩余量
```

关键点：`vl` 是**运行时**算出来的，程序员**不写死**。这正是 RVV 代码可移植的根源——同一份循环在 VLEN=128 和 VLEN=512 上都正确，只是每次循环 `vl` 不同、循环次数不同。

#### 4.1.3 源码精读

`sw/opt/rvv_opt.h` 的 `Memcpy` 是这套写法的范本。先看它的整体结构（命名空间 `coralnpu_v2::opt`）：

[sw/opt/rvv_opt.h:23-38](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/rvv_opt.h#L23-L38) —— `Memcpy` 用 `__riscv_vsetvl_e8m8(n)` 动态求 `vl`，再 `vle8`/`vse8` 搬运，循环推进。

逐行拆解关键三句：

[sw/opt/rvv_opt.h:29-34](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/rvv_opt.h#L29-L34) ——
- `vl = __riscv_vsetvl_e8m8(n);`：intrinsic 形式，名字里 `e8m8` 表示 SEW=8、LMUL=m8，参数 `n` 是 AVL，返回值就是本次 `vl`。
- `vload_data = __riscv_vle8_v_u8m8(s, vl);`：从源地址加载 `vl` 个 `uint8` 到一个 `vuint8m8_t` 向量组。
- `__riscv_vse8_v_u8m8(d, vload_data, vl);`：把向量组写回目的地址。
- `s += vl; d += vl; n -= vl;`：推进指针、缩小剩余量，进入下一轮。

`Memset` 略有不同——它先 `vsetvl` 一次、用 `__riscv_vmv_v_x_u8m8(v, vl)` 把一个标量广播成整组向量，再在循环里反复 store 这同一组向量：

[sw/opt/rvv_opt.h:44-52](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/rvv_opt.h#L44-L52) —— `vmv_v_x` 是「标量→向量」的广播，填充值只需构造一次。

> 旁注：这两个函数用 `inline` 声明在头文件里、放进 `coralnpu_v2::opt` 命名空间，是给 ML 算子库（见 u10-l2）当「向量化 memcpy/memset」直接调用的轻量工具。

#### 4.1.4 代码实践

1. **实践目标**：亲手感受 `vsetvl` 的「问容量」语义，验证 `vl` 随剩余量变化。
2. **操作步骤**：
   - 打开 [sw/opt/rvv_opt.h](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/rvv_opt.h)，在 `Memcpy` 的循环里临时加一行把 `vl` 写到一个全局变量，例如 `g_last_vl = vl;`（**示例代码**，仅用于观测，勿提交）。
   - 想象调用 `Memcpy(dst, src, 100)`：VLEN=128、SEW=8、LMUL=m8 ⇒ VLMAX = 128×8/8 = 128。第一轮 AVL=100 < 128，故 `vl=100`；循环一次即结束。
   - 再想象 `Memcpy(dst, src, 300)`：第一轮 `vl=128`（被 VLMAX 截断），剩 172；第二轮 `vl=128`，剩 44；第三轮 `vl=44`。
3. **需要观察的现象**：`vl` 永远满足 `vl = min(剩余量, VLMAX)`，且各轮 `vl` 之和恰好等于总长度。
4. **预期结果**：300 = 128 + 128 + 44。这正是软件 stripmining 把任意长度数组「啃」干净的保证。
5. **运行结果**：本机若无 RISC-V 工具链运行环境，上述数值演算可手工完成；实际在仿真器上跑需待 u2-l3/u10-l3。**待本地验证**实际 `vl` 序列。

#### 4.1.5 小练习与答案

**练习 1**：`__riscv_vsetvl_e8m8(n)` 中 `e8m8` 分别代表什么？为什么函数名要把它们写死？

**参考答案**：`e8` = SEW=8（8 位元素），`m8` = LMUL=m8（8 个寄存器一组）。这是 intrinsic 的「类型化」封装——它把 vtype 的一部分编码进函数名，让编译器能推断返回的 `vl` 与配套的向量类型 `vuint8m8_t` 严格匹配，避免程序员手写 `vtype` 立即数出错。

**练习 2**：如果把 `Memcpy` 里的 `m8` 改成 `m1`，循环次数会变多还是变少？吞吐呢？

**参考答案**：`m1` 的 VLMAX = 128/8 = 16，远小于 `m8` 的 128，所以**每次循环处理的字节数变少、循环次数变多**。吞吐通常下降，因为同样多的数据需要更多条向量指令、更多前端派发压力（这正是 overview.md 强调「用分组提升单指令吞吐」的原因）。

### 4.2 向量加载、运算与存储的完整范例

#### 4.2.1 概念说明

`rvv_add_intrinsic.cc` 是一个极简却完整的向量程序：把两个 int8 数组**逐元素相加并存成 int16**（加宽 widening）。它演示了 RVV 程序的三段式结构：

1. **加载**（load）：用 `vle` 把内存里的标量数组搬进向量寄存器组。
2. **运算**（compute）：对向量组做算术，这里是「加宽加法」`vwadd`——两个窄输入（int8）相加，结果自动扩展成宽类型（int16）。
3. **存储**（store）：用 `vse` 把向量结果写回内存。

注意「加宽」类指令的强约束：**输出向量的元素宽度必须是输入的 2 倍，且输出 LMUL = 2 × 输入 LMUL**。范例里输入是 `vint8m4_t`、输出是 `vint16m8_t`，正好满足 m8 = 2×m4、16bit = 2×8bit。

#### 4.2.2 核心流程

```text
input_1[1024], input_2[1024] : int8   # 输入（运行时 memset 填充）
output[1024]              : int16     # 输出

for idx in 0, 32, 64, ..., 992:       # 每轮处理 32 个元素
    v2 = vle8(input_2 + idx, vl=32)   # 加载 32 个 int8 → vint8m4_t
    v1 = vle8(input_1 + idx, vl=32)   # 加载 32 个 int8 → vint8m4_t
    s  = vwadd(v1, v2, vl=32)         # 加宽加法 → vint16m8_t
    vse16(output + idx, s, vl=32)     # 存回 32 个 int16
```

这里有个**和 4.1 的重要区别**：范例**没有用 `vsetvl` 动态求 `vl`**，而是直接写死 `vl=32`，并用一个普通的 C `for` 循环以步长 32 推进。这是一种**简化的教学写法**——它要求程序员自己保证 `32 ≤ VLMAX`（对 `int8m4`，VLMAX=64，满足；对输出 `int16m8`，VLMAX=64，也满足）。生产代码（如 rvv_opt.h）才会用 `vsetvl` 让 `vl` 自适应。

#### 4.2.3 源码精读

先看缓冲区声明与初始化：

[examples/rvv_add_intrinsic.cc:18-28](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/examples/rvv_add_intrinsic.cc#L18-L28) —— 三个全局数组（无初值），`main` 里用 `memset` 把两个输入分别填成全 1、全 6。这里没用 `__attribute__((section(".data")))`，因为程序**自己**用 `memset` 初始化输入、不依赖外部主机注入；数组作为普通全局变量进 `.bss` 即可（若要由 cocotb 主机预填输入，则需 `section(".data")` 钉进 DTCM，见 [u2-l2](u2-l2-write-compile-program.md)）。

核心循环：

[examples/rvv_add_intrinsic.cc:30-37](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/examples/rvv_add_intrinsic.cc#L30-L37) —— 这是整段程序的心脏，逐行：

- 循环条件 `(idx + 31) < 1024` 配合 `idx += 32`：保证每轮处理连续 32 个元素，且不越界（最后一轮 idx=992，992+31=1023 < 1024）。共 1024/32 = 32 轮。
- [L31-32](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/examples/rvv_add_intrinsic.cc#L31-L32) `__riscv_vle8_v_i8m4(ptr, 32)`：加载 32 个 **int8** 到 `vint8m4_t`。函数名解码：`vle` = vector load，`8` = 元素 8 位，`i8m4` = 有符号 int8、LMUL=m4。第三参 `32` 就是 `vl`。
- [L34](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/examples/rvv_add_intrinsic.cc#L34) `__riscv_vwadd_vv_i16m8(v1, v2, 32)`：**w**add = widening add，`vv` = 两个向量操作数，`i16m8` = 结果是 int16、LMUL=m8。两个 int8 相加，结果自动扩成 int16，故 1+6=7 写成 `0x0007`。
- [L36](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/examples/rvv_add_intrinsic.cc#L36) `__riscv_vse16_v_i16m8(ptr, s, 32)`：存 32 个 **int16** 回 output。

构建规则把这段源码变成可加载镜像：

[examples/BUILD.bazel:24-27](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/examples/BUILD.bazel#L24-L27) —— `coralnpu_v2_binary` 规则（见 u1-l3/u2-l2）自动切到 RISC-V 裸机平台、链接 CRT、生成 `.elf/.bin/.vmem`。intrinsic 不需要任何额外依赖——`<riscv_vector.h>` 由 multilib 工具链（默认 `rv32imf`，含 Zve32x 向量子集）直接提供。

#### 4.2.4 代码实践

1. **实践目标**：把范例跑过编译，并验证 intrinsic 确实被编成了向量指令。
2. **操作步骤**：
   - 在仓库根目录执行：
     ```bash
     bazel build //examples:coralnpu_v2_rvv_add_intrinsic
     ```
   - 找到产物里的 `.elf`（路径形如 `bazel-out/.../examples/coralnpu_v2_rvv_add_intrinsic.elf`）。
   - 用工具链的反汇编查看向量指令（路径以本机为准，**示例命令**）：
     ```bash
     <riscv32-toolchain-prefix>-objdump -d <path-to>.elf | grep -E 'vle|vse|vwadd'
     ```
3. **需要观察的现象**：反汇编里应出现 `vle8.v`、`vwadd.vv`、`vse16.v` 这类向量指令，证明 intrinsic 被编译成了真实的 RVV 指令，而非标量回退。
4. **预期结果**：能看到向量 load/store 与 widening add 的反汇编。
5. **运行结果**：若无现成 `objdump` 路径，可在 `bazel-out` 里搜索工具链产物，或先跳过反汇编只确认 `bazel build` 成功。**待本地验证**具体反汇编输出。

#### 4.2.5 小练习与答案

**练习 1**：范例里输出类型为什么是 `vint16m8_t` 而不是 `vint8m4_t`？

**参考答案**：因为 `vwadd` 是**加宽**指令，结果元素宽度 = 2 × 输入宽度（int8 → int16），且输出 LMUL = 2 × 输入 LMUL（m4 → m8）。若强行用 `vint8m4_t` 接收结果，类型不匹配、编译报错；即便不报错也会截断溢出信息。

**练习 2**：范例把 `vl` 写死成 32。请算出 `vint8m4_t` 在 VLEN=128 下的 VLMAX，并说明 `vl=32` 是否安全、是否最优。

**参考答案**：VLMAX = 128×4/8 = 64。`vl=32 ≤ 64` 安全；但只用了半个寄存器组容量，**不是最优**——改成 `vl=64`（并把循环步长、条件相应改成 64）能让每条向量指令吞吐翻倍。教学版用 32 是为了数字清爽。

### 4.3 向量类型确认：rvv_type_util 与自动生成头

#### 4.3.1 概念说明

写 intrinsics 时最常踩的坑是：「我选的这个类型（比如 `vint16m2_t`）到底能装几个元素？配套的 `vsetvl` 该用哪个？」 CoralNPU 提供了两个工具来消除猜测：

- **`rvv_type_util.py`**：一个纯 Python 的「**黄金查表模型**」。它把 dtype→SEW、(SEW, LMUL)→VLMAX、以及硬件 `vtype` 寄存器的位编码全部列成字典，**和硬件 RTL 共用同一套编码定义**。cocotb 测试用它构造期望值与 vtype。
- **`rvv_cpp_util_header_generator.py`**：一个**代码生成器**。它枚举所有 (位宽 × 有无符号 × LMUL) 组合，自动生成一份 C++ 头 `rvv_cpp_util.h`，把上百个 `__riscv_*` intrinsic 包成模板函数（如 `Vadd<T, lmul>(...)`），让测试代码用「类型+LMUL」泛型地写一遍、覆盖所有数据类型。

#### 4.3.2 核心流程

`rvv_type_util.py` 的工作链：

```text
numpy dtype ──(DTYPE_TO_SEW)──▶ SEW 编码(0b000/001/010)
SEW + LMUL   ──(SEW_TO_LMULS_AND_VLMAXS)──▶ 该组合的 VLMAX
SEW + LMUL + ma/ta ──(construct_vtype)──▶ 硬件 vtype 寄存器值
```

`rvv_cpp_util_header_generator.py` 的工作链：

```text
枚举 (bit_count × signed × lmul)
   ├── 为每个组合生成 RvvTypeTraits<T, lmul>（把 T+lmul 映射到 v...m.._t）
   ├── 为每个算子(vadd/vmul/...)生成 SameTypeBinaryOpTraits（绑定具体 intrinsic 函数指针）
   └── 生成模板包装器 Vadd<T,lmul>(...) 统一调用
▶ 输出 rvv_cpp_util.h（被 cocotb 的 .cc 测试 #include）
```

#### 4.3.3 源码精读

先看 `rvv_type_util.py` 的三张核心表。

dtype → SEW 编码：

[coralnpu_test_utils/rvv_type_util.py:18-22](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_type_util.py#L18-L22) —— `uint8→0b000`、`uint16→0b001`、`uint32→0b010`，正好对应 vtype 里 SEW 字段的 3 位编码（SEW = 8 << field）。

(SEW, LMUL) → VLMAX 查表（注意：这张表里的 LMUL 编码是 **CoralNPU 自定义的**，不是标准 RVV 的 000=MF8 序列）：

[coralnpu_test_utils/rvv_type_util.py:30-52](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_type_util.py#L30-L52) —— 以 SEW=8（`0b000`）行为例：LMUL 编码 `0b000`→VLMAX 16、`0b001`→32、`0b010`→64、`0b011`→128。反推 VLEN：VLMAX = VLEN×LMUL/SEW，代入 (LMUL=1, VLMAX=16, SEW=8) 得 **VLEN=128**，与 RTL 默认 `-DVLEN_128` 一致。这张表就是「我选某类型，最多能装几个元素」的权威答案。

把 vtype 各字段拼成一个寄存器值：

[coralnpu_test_utils/rvv_type_util.py:63-65](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_type_util.py#L63-L65) —— `construct_vtype(ma, ta, sew, lmul)` 按 `vtype` 的位布局 `(ma<<7)|(ta<<6)|(sew<<3)|lmul` 拼装，与硬件 CSR 完全对齐（ma/ta 是尾元素与未活动元素的处理策略）。

再看 C++ 头生成器。它先定义 `Lmul` 枚举与 `MAX_VREG_GROUP_BYTES`：

[coralnpu_test_utils/rvv_cpp_util_header_generator.py:41-53](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_cpp_util_header_generator.py#L41-L53) —— `MAX_VREG_GROUP_BYTES = 128` 正好是 VLEN=128 位 = 128 字节，即 m8 组的最大字节数；`Lmul` 枚举列出 MF4/MF2/M1/M2/M4/M8。

核心是「为每个 (类型, LMUL) 生成一个 trait，把 `T+lmul` 映射到具体 `v...m.._t`」：

[coralnpu_test_utils/rvv_cpp_util_header_generator.py:588-596](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_cpp_util_header_generator.py#L588-L596) —— 循环枚举所有 (bit_count, lmul, signed)，生成形如 `template<> struct RvvTypeTraits<int16_t, Lmul::M2>{ using type = vint16m2_t; };` 的特化，再配上 `RvvType<T,lmul>` 别名，让上层用 `RvvType<int16_t, Lmul::M2>` 统一指代 `vint16m2_t`。

这份自动生成的头在 cocotb 测试里被这样消费（一个真实用例）：

[tests/cocotb/rvv/arithmetics/rvv_vx_arithmetics.cc:5](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/rvv/arithmetics/rvv_vx_arithmetics.cc#L5) —— `#include "coralnpu_test_utils/rvv_cpp_util.h"`，随后即可调用 `Vle<T,lmul>`、`Vadd<T,lmul>` 等模板包装器，一份代码覆盖所有数据类型。

> 旁注：这两份工具是**测试基础设施**，不是内核运行时依赖。普通业务程序（如范例）直接用 `<riscv_vector.h>` 即可；只有当你需要大规模、跨数据类型地写向量算子测试时，才用生成头省去手写几百个特化的重复劳动。

#### 4.3.4 代码实践

1. **实践目标**：用 `rvv_type_util.py` 确认 `vint16m2_t` 的 VLMAX，验证你写 intrinsics 时选的 `vl` 上限。
2. **操作步骤**：
   - 在仓库根目录启动 Python（**示例命令**，需能 import numpy）：
     ```bash
     python3 -c "from coralnpu_test_utils.rvv_type_util import DTYPE_TO_SEW, SEW_TO_LMULS_AND_VLMAXS; import numpy as np; sew=DTYPE_TO_SEW[np.uint16]; print('sew=',sew); print(SEW_TO_LMULS_AND_VLMAXS[sew])"
     ```
   - 也可以直接运行生成器看产出头：
     ```bash
     python3 coralnpu_test_utils/rvv_cpp_util_header_generator.py | head -40
     ```
3. **需要观察的现象**：第一行打印出 `sew= 1`（即 0b001，uint16）；第二行打印 SEW=16 的 (LMUL, VLMAX) 列表，其中 LMUL 编码 `0b001`（M2）对应 VLMAX=16。
4. **预期结果**：确认 `vint16m2_t` 在 VLEN=128 下 VLMAX=16，即 `vsetvl_e16m2(n)` 返回的 `vl` 最大为 16。
5. **运行结果**：若本机缺 numpy 或 import 路径不通，可改为查表手工核对：SEW=16、M2 ⇒ VLMAX = 128×2/16 = 16。**待本地验证**脚本输出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `rvv_type_util.py` 要自己维护一张 LMUL→VLMAX 表，而不是直接套用标准 RVV 公式？

**参考答案**：因为 CoralNPU 的 vtype 里 LMUL 字段**采用了自定义编码**（见 u7-l6：为给 64 个向量寄存器腾出 6 位索引，C 扩展编码空间被复用，类型编码也做了定制），与标准 RVV 的 `000=MF8/011=M1/...` 序列不同。把自定义编码与 VLMAX 直接列成表，既当文档又当黄金模型，保证 Python 期望值与 RTL 行为一致。

**练习 2**：`rvv_cpp_util_header_generator.py` 生成的头里，`RvvType<int16_t, Lmul::M2>` 会被解析成哪个具体类型？

**参考答案**：`vint16m2_t`。生成器在 [L588-596](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_cpp_util_header_generator.py#L588-L596) 为每个组合生成了 `RvvTypeTraits` 特化，命名规则是 `v` + `(u)int` + bit_count + lmul(小写) + `_t`，故 `int16` + `M2` → `vint16m2_t`。

## 5. 综合实践

把本讲三个知识点串起来：**用 intrinsics 写一个「两个 int16 数组逐元素相加」的向量程序，用 `rvv_type_util` 确认类型容量，用 `coralnpu_v2_binary` 编译。**

1. **新建源文件** `examples/my_rvv_add_i16.cc`（**示例代码**，需自行创建）：
   ```cpp
   #include <riscv_vector.h>
   #include <string.h>

   int16_t input_1[512];
   int16_t input_2[512];
   int16_t output[512];

   int main() {
     memset(input_1, 1, sizeof(input_1));
     memset(input_2, 2, sizeof(input_2));

     const int16_t* p1 = input_1;
     const int16_t* p2 = input_2;
     int16_t* po = output;
     size_t n = 512;

     while (n > 0) {
       size_t vl = __riscv_vsetvl_e16m2(n);            // 动态求 vl，SEW=16、LMUL=m2
       vint16m2_t a = __riscv_vle16_v_i16m2(p1, vl);   // 加载 vl 个 int16
       vint16m2_t b = __riscv_vle16_v_i16m2(p2, vl);
       vint16m2_t s = __riscv_vadd_vv_i16m2(a, b, vl); // 逐元素相加（同宽，非加宽）
       __riscv_vse16_v_i16m2(po, s, vl);               // 存回
       p1 += vl; p2 += vl; po += vl; n -= vl;
     }
     return 0;
   }
   ```
2. **在 `examples/BUILD.bazel` 追加目标**（**示例代码**）：
   ```python
   coralnpu_v2_binary(
       name = "coralnpu_v2_my_rvv_add_i16",
       srcs = ["my_rvv_add_i16.cc"],
   )
   ```
3. **用 `rvv_type_util` 确认类型**：如 4.3.4 所示，SEW=16、M2 ⇒ VLMAX=16。所以 `vsetvl_e16m2(512)` 第一轮返回 16，循环共 512/16 = 32 轮。这验证了你选的 `vint16m2_t` 容量足够、且 `vl` 不会越界。
4. **编译**：
   ```bash
   bazel build //examples:coralnpu_v2_my_rvv_add_i16
   ```
5. **自检**（可选）：用 `objdump -d` 反汇编，确认出现 `vle16.v` / `vadd.vv` / `vse16.v` / `vsetvli` 指令。

**验收标准**：`bazel build` 成功产出 `.elf`；`rvv_type_util` 查得的 VLMAX=16 与你循环里 `vl` 的上限一致；反汇编能看到向量指令。运行该程序（在 Verilator 或 cocotb 上加载并 halt）留待 [u2-l3](u2-l3-run-on-verilator.md) 与 [u10-l3](u10-l3-npusim-mobilenet.md)。

## 6. 本讲小结

- RVV 编程模型的核心是 **`vsetvli`**：它把 SEW/LMUL 写进 `vtype`，并算出本次 `vl = min(AVL, VLMAX)`，让程序「按硬件容量自适应地处理元素」。三者的容量公式是 \(\text{VLMAX} = \text{VLEN}\times\text{LMUL}/\text{SEW}\)。
- **软件 stripmining** = `while(n>0){ vl=vsetvl(n); 处理vl个; n-=vl; }`。[sw/opt/rvv_opt.h](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/sw/opt/rvv_opt.h) 的 `Memcpy`/`Memset` 是这套「动态 vl」写法的范本；它和 u7-l6 讲的**硬件** stripmining（一次派发→四次发射）是两个不同层面。
- [rvv_add_intrinsic.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/examples/rvv_add_intrinsic.cc) 演示了向量程序三段式（load→compute→store），并用 `vwadd` 展示了**加宽**指令的强约束：输出宽度=2×输入宽度、输出 LMUL=2×输入 LMUL。它写死 `vl=32` 是教学简化，生产代码应改用 `vsetvl`。
- intrinsic 的命名可机械解码：`__riscv_vle8_v_i8m4` = 向量 load、元素 8 位、`vv`/`v` 变体、有符号 int8、LMUL=m4。
- [rvv_type_util.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_type_util.py) 是「向量类型容量 + vtype 编码」的 Python 黄金模型，反推出 VLEN=128，与 RTL `-DVLEN_128` 一致；[rvv_cpp_util_header_generator.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/coralnpu_test_utils/rvv_cpp_util_header_generator.py) 自动生成类型安全的 C++ 算子封装头，供 cocotb 测试大规模复用。
- CoralNPU 生产配置 **VLEN=128**、向量核支持 8/16/32 位元素；`<riscv_vector.h>` 由 multilib 工具链（默认 `rv32imf`/Zve32x）直接提供，业务程序无需额外依赖即可用 intrinsics。

## 7. 下一步学习建议

- **[u10-l2 litert-micro 算子内核](u10-l2-litert-micro-kernels.md)**：看真实的 ML 算子（conv / depthwise_conv / fully_connected）如何把权重和激活组织成 MAC 外积引擎的 wide/narrow 输入，并大量使用本讲讲的 intrinsics 与 `rvv_opt.h`。这是 intrinsics 走向「真正干活」的下一站。
- **[u10-l3 npusim 与 MobileNet 端到端](u10-l3-npusim-mobilenet.md)**：把本讲编出来的向量程序加载到 `coralnpu_v2_sim` 仿真器里实际跑一遍，观察 halt 与输出。
- **重读 [u7-l6 Stripmining 与 SIMD 指令编码](u7-l6-stripmining-encoding.md)**：把本讲的**软件** stripmining 与硬件侧的「一次派发→四次发射」对照阅读，理解两层 stripmining 如何协作。
- **若对硬件侧好奇**：回到 [u7-1 ~ u7-5](u7-l1-rvv-backend-overview.md) 看 RTL 如何译码、派发、执行这些向量指令——本讲的每一条 intrinsic 最终都落在那里。
