# 浮点数据类型的 CPU 仿真

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么 cpudebug 要在 CPU 上「重新发明」fp16/bf16/fp8/fp4/hif8 这些 NPU 专有数据类型。
- 区分两类仿真类型——「计算型（fp16/bf16）」与「传输型（fp8/fp4/hif8）」——并理解它们在运算符集合上的显著差异。
- 读懂 `struct half`、`struct Bf16T` 以及各 fp8/fp4/hif8 头文件里的位布局定义。
- 跟踪一次 `float → fp16` 的舍入转换过程，理解「就近偶舍入（round-to-nearest-even）」对仿真保真度的意义。
- 结合最近一次提交（为 `half`/`Bf16T` 补全一元 `operator-()`），解释「补全一元运算符」对类型完备性与 IEEE-754 语义的价值。

## 2. 前置知识

本讲假设你已经建立以下认知（来自前置讲义）：

- **CPU 域孪生调试**：cpudebug 让同一份 Ascend C 源码不改一行就能在 CPU 上跑起来（见 u2-l1）。
- **stub 注册机制**：源码里的内建函数（`Add`、`DataCopy` 等）在 CPU 域通过 `(fid, type)` 二维函数表绑定到可执行代码（见 u3-l3）。
- **闭源/开源边界**：cpudebug 由开源部分（`acl_stub`、`api_check`、`regfwk`）与闭源模型库（`libcpudebug_model.a`）链接而成；有些实现并不出现在开源树里。

此外，补充两个本讲要用到的通用概念：

- **IEEE 754 浮点格式**：一个浮点数由「符号位 S + 指数 E + 尾数 M」组成。对于**规格化**数，其值为：

  \[ v = (-1)^{S} \times 2^{E - \text{bias}} \times (1 + \frac{M}{2^{M_{\text{len}}}}) \]

  对于**非规格化**数（E=0），隐含的前导位是 0 而非 1：

  \[ v = (-1)^{S} \times 2^{1 - \text{bias}} \times (0 + \frac{M}{2^{M_{\text{len}}}}) \]

  其中 `bias` 是指数偏置。不同格式（fp16/bf16/fp8/...）的区别，本质上就是 S/E/M 三段的位宽与 bias 不同。

- **C++ 自定义数值类型**：为了让一个 `struct` 表现得像内置数值类型，需要重载算术、比较、赋值、类型转换等运算符。本讲会看到 cpudebug 为这些低精度类型精心设计了一套运算符。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [cpudebug/include/kernel_fp16.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp16.h) | 定义 `struct half`（fp16）及其全部运算符声明、位运算辅助宏与模板；是 16 位计算型的核心 |
| [cpudebug/include/kernel_bf16.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_bf16.h) | 定义 `struct Bf16T`（bf16）及其运算符声明；实现位于闭源模型库 |
| [cpudebug/src/acl_stub/kernel_fp16.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp) | `half` 的开源实现：`FloatToFp16`/`Fp16ToFloat` 转换、`Fp16Add`/`Fp16Sub` 等位级运算、各类运算符函数体 |
| [cpudebug/include/kernel_vectorized.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_vectorized.h) | 汇总引入 fp16/bf16/fp8/hif8 头文件，并定义 `half2`/`bfloat16x2_t`/`float8_e4m3x2_t` 等「成对打包」类型 |
| [cpudebug/include/kernel_fp8_e4m3.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp8_e4m3.h) 等 | fp8（e4m3 / e5m2 / e8m0）、fp4（e2m1 / e1m2）、hif8 等传输型格式的位布局与转换声明 |

这些头文件的引入链是：`cpu_debug_launch.h` → `tikicpulib.h` → `kernel_fp16.h` + `kernel_bf16.h` + `kernel_vectorized.h`（后者再引入 fp8/hif8）。也就是说，用户在算子里用到的 `half`、`bfloat16_t`、`fp8_e4m3fn_t` 等类型，最终都由这条链路一次性带入 CPU 编译。

> 引用：[cpudebug/include/tikicpulib.h:L18-L20](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/tikicpulib.h#L18-L20) 一次性把 fp16/bf16/vectorized 三个头文件纳入 CPU 域编译。

---

## 4. 核心概念与源码讲解

### 4.1 fp16 与 bf16：16 位「计算型」仿真类型

#### 4.1.1 概念说明

NPU 的向量单元原生支持 fp16（半精度）和 bf16（脑浮点）两种 16 位浮点格式，但标准 C++ 的 CPU 既没有 `half` 也没有 `bfloat16` 类型。如果 CPU 仿真要可信，就必须「重新发明」这两个类型，让它们满足两个要求：

1. **位布局与 NPU 完全一致**——同一个 16 位比特模式，在 CPU 和 NPU 上解释出的数值必须相同，否则孪生调试就失去了意义。
2. **行为与内置浮点足够接近**——能参与 `+`/`-`/`*`/`/`、比较、赋值、隐式转换，让 Ascend C 源码在 CPU 上自然编译通过。

实现手法是一个经典 C++ 惯用法：**用一个 `uint16_t` 成员持有原始比特，再围绕它重载一整套运算符**。

> 引用：[cpudebug/include/kernel_fp16.h:L216-L225](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp16.h#L216-L225) 给出 fp16 的位布局注释，并定义 `struct half { uint16_t val; ... }`——注意它只有一个 16 位成员 `val`，所有运算符都围绕 `val` 展开。

这里要建立一个贯穿本讲的关键区分：

- **计算型类型（fp16/bf16）**：拥有**完整的算术运算符**（`+ - * /`、`+=`、`++`、比较等）。它们在 CPU 仿真里会被「当成数值」直接参与标量计算。
- **传输型类型（fp8/fp4/hif8）**：**几乎没有算术运算符**，只提供「构造 + 赋值 + 转回 float/bf16」。它们只负责忠实搬运比特，真正的运算由 NPU 指令完成（见 4.2）。

fp16 与 bf16 都是 16 位，但取舍不同：

| 类型 | 符号位 S | 指数位 E | 尾数位 M | 指数偏置 bias | 特点 |
| --- | --- | --- | --- | --- | --- |
| fp16 (`half`) | 1 | 5 | 10 | 15 | 数值范围小、精度高，深度学习常用 |
| bf16 (`Bf16T`) | 1 | 8 | 7 | 127 | 与 fp32 同范围、精度低，便于 fp32↔bf16 截断转换 |

> 引用：[cpudebug/include/kernel_fp16.h:L88-L103](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp16.h#L88-L103) 用枚举 `Fp16BasicParam` 集中定义 fp16 的 bias=15、各段位宽与掩码；[cpudebug/include/kernel_bf16.h:L23-L31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_bf16.h#L23-L31) 用 `constexpr` 定义 bf16 的对应常量（指数段 8 位、尾数段 7 位）。

注意 bf16 复用了 fp32 的指数布局（同为 8 位指数、bias=127），所以 `BF16_EXP_BIAS` 直接取自 `FP32_EXP_BIAS`：

> 引用：[cpudebug/include/kernel_bf16.h:L59-L85](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_bf16.h#L59-L85) 定义 fp32 的参数，并令 `BF16_EXP_BIAS = FP32_EXP_BIAS`——这正是「bf16 = fp32 截断低 16 位尾数」这一转换关系的根源。

#### 4.1.2 核心流程

一个 `half` 对象从「诞生」到「参与运算」再到「还原回可读数值」，经历三步：

```text
[float 输入]
   │  构造: half h(fVal)  →  FloatToFp16(fVal)
   ▼
[uint16_t val]  ←—— 比特布局与 NPU 一致，这一步是「定影」
   │  算术: h1 + h2  →  Fp16Add(h1.val, h2.val)  （位级运算）
   │        h1 - h2  →  Fp16Sub(h1.val, h2.val)
   ▼
[uint16_t val]  ←—— 运算结果仍是 16 位比特
   │  还原: (float)h  →  Fp16ToFloat(h.val)
   ▼
[float 输出]
```

要点：

- **构造**时立即把 `float` 压缩成 16 位并定影到 `val`，之后对象里只存比特、不再存 float。
- **算术运算**不在 `float` 域里做，而是直接对 `uint16_t` 做位级运算（提取 S/E/M、对阶、舍入、重组），这样才能与 NPU 的逐位行为一致。
- **还原**时再把 16 位比特展开回 `float`，供宿主侧打印、比对精度。

bf16 的流程完全对称，只是实现函数换成 `FloatToBf16`/`Bf16Add`/`Bf16Sub` 等。

#### 4.1.3 源码精读

**（1）`struct half` 的运算符集合**

`half` 的运算符非常完整——二元 `+ - * /`、复合赋值 `+= -= *= /=`、自增自减 `++ --`、比较 `== != > >= < <=`、逻辑 `&& ||`、一元负号 `-`，以及到 8 种内置类型的转换运算符：

> 引用：[cpudebug/include/kernel_fp16.h:L294-L307](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp16.h#L294-L307) 依次声明 `operator+` 与一元 `operator-()`、二元 `operator-`。其中一元 `half operator-() const;` 正是最近一次提交新增的（见 4.1.4）。

> 引用：[cpudebug/include/kernel_fp16.h:L508-L555](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp16.h#L508-L555) 声明到 `float`/`double`/`int8_t` 等类型的转换运算符，使 `half` 能在需要时隐式拓宽为内置数值类型。

**（2）`struct Bf16T` 的对称结构**

`Bf16T` 的结构与 `half` 高度同构——同样的「单个 `uint16_t val` 成员 + 一堆运算符」：

> 引用：[cpudebug/include/kernel_bf16.h:L143-L214](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_bf16.h#L143-L214) 定义 `struct Bf16T`，运算符集合比 `half` 精简（只保留 `= + += - -=` 与到 `float` 的转换），但同样含一元 `operator-()`。

> 引用：[cpudebug/include/kernel_bf16.h:L178-L184](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_bf16.h#L178-L184) 在 `operator+=` 之后声明本次新增的 `Bf16T operator-() const;`。

注意两类边界：

- `half` 的运算符**实现**是开源的，集中在 `kernel_fp16.cpp`。
- `Bf16T` 的运算符**只有声明**在头文件里，整个 `Bf16T` 没有任何开源 `.cpp` 提供函数体——它的实现位于闭源模型库 `libcpudebug_model.a`。这一点会在 4.1.4 的实践中亲自验证。

**（3）二元减法 = 「翻转符号位 + 加法」**

`half` 的二元 `operator-` 调用 `Fp16Sub`，而 `Fp16Sub` 的实现揭示了一个 IEEE 754 的关键性质——**`a - b` 等价于 `a + (-b)`，而 `-b` 就是把 `b` 的符号位取反**：

> 引用：[cpudebug/src/acl_stub/kernel_fp16.cpp:L667-L673](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L667-L673) `Fp16Sub` 构造 `tmp = (~v2) & SIGN_MASK | (v2 & ABS_MAX)`——即「翻转 v2 的最高符号位、其余 15 位不变」，再交给 `Fp16Add`。

> 引用：[cpudebug/src/acl_stub/kernel_fp16.cpp:L805-L811](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L805-L811) 给出二元 `half half::operator-(const half fp) const` 的函数体，它只是把两个 `val` 喂给 `Fp16Sub` 再包回 `half`。

这段代码为理解「一元 `operator-()` 应当如何实现」提供了直接线索：按同样的位翻转思路，一元负号的自然实现就是把 `val` 的符号位异或 `0x8000`（详见 4.1.4）。

#### 4.1.4 代码实践

**实践目标**：对比 `kernel_fp16.h` 与 `kernel_bf16.h` 的结构，结合最近提交 `c6f35b0`（为 `struct half`/`Bf16T` 补全一元 `operator-()`），说明「为这些类型补全一元运算符」的意义，并亲自核对该实现的开源/闭源归属。

**操作步骤**：

1. 查看本次提交只动了哪些文件：
   ```bash
   git show c6f35b0 --stat
   ```
   预期：仅 `kernel_fp16.h`、`kernel_bf16.h` 两个头文件，共 12 行新增，且全部是 `operator-()` 的**声明**。

2. 看具体 diff：
   ```bash
   git show c6f35b0 -- cpudebug/include/kernel_fp16.h cpudebug/include/kernel_bf16.h
   ```
   观察：新增的分别是 `half operator-() const;` 与 `Bf16T operator-() const;`，紧跟在各自的二元 `operator+`/`operator+=` 之后。

3. 在开源树里查找它的**函数体**：
   ```bash
   grep -rn "operator-()" cpudebug/
   grep -rn "half::operator-\|Bf16T::" cpudebug/src/
   ```
   观察：`operator-()` 在整个 `cpudebug/` 下只有两处命中——都是头文件里的声明；`kernel_fp16.cpp` 里只有二元 `half::operator-`、`half::operator-=`、`half::operator--`，**没有**一元 `half::operator-()` 的函数体；`Bf16T::` 的任何方法都搜不到开源实现。

4. 对照阅读 `Fp16Sub`（[L667-L673](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L667-L673)），确认「翻转符号位」就是 IEEE 754 取负的位级实现。

**需要观察的现象**：

- 提交只改了头文件，没有改任何 `.cpp`。
- `half` 的二元运算符是开源的，但新增的一元 `operator-()` 在开源 `.cpp` 中找不到函数体；`Bf16T` 则连二元运算符的实现都不在开源树里。

**预期结果与解释**：

「为 `half`/`Bf16T` 补全一元 `operator-()`」的意义至少有三层：

1. **类型语义完备性**。一个声明了二元 `a - b` 的数值类型，若缺少一元 `-x`，会让 `-x`、`a + (-b)` 这类表达式要么编译失败，要么被迫走隐式转换链（`half → float → 取负 → half`），结果类型从 `half` 漂移成 `float`，破坏模板/泛型代码的类型推导。
2. **IEEE 754 取负的正确语义**。取负应当是**纯符号位翻转**，而非「`0 - x`」或「转 float 再取负」。这两者并不等价：
   - 对 **+0.0** 取负应得到 **−0.0**（符号位翻转天然成立）；而 `0 - (+0)` 在就近舍入下得到 **+0.0**，丢失了符号零信息。
   - 对 **NaN**，符号位的含义由其 payload 决定，位翻转能保留 payload，而经由 `float` 的往返可能改变 NaN 的编码。
   因此一个以「异或符号位」实现的一元 `operator-()`，是唯一能逐位对齐 NPU 取负指令的写法。
3. **与 stub/泛型代码协作**。u3-l3 提到内建函数由脚本批量生成绑定；当生成代码或用户模板里出现 `-val` 时，只有类型本身提供了一元 `operator-()`，实例化才能成功。

**关于实现归属（待本地验证）**：在当前开源树中，`half::operator-()` 与 `Bf16T::*` 的函数体都未出现。这有两种可能，且都能解释「本地冒烟通过」：

- 函数体在**闭源模型库** `libcpudebug_model.a` 中提供，链接期被解析；
- 暂无任何代码 **odr-use** 该一元运算符（C++ 中未被实际调用的成员函数无需定义即可通过编译）。

你可以在本地编译产物上用 `nm`/`objdump` 验证：

```bash
nm -C <path>/libcpudebug.so | grep "half::operator-"
```

若能看到 `half::operator-() const` 的符号定义，则确认其由闭源库提供；若只能看到二元 `operator-` 而看不到一元符号，则说明当前尚无调用点。无论哪种，都**不应**在没有确认的情况下，把一元 `operator-()` 当作已实现来调用。

#### 4.1.5 小练习与答案

**练习 1**：`half` 已经有二元 `operator-`（可写 `a - b`），为什么还要单独加一元 `operator-()`？直接写 `0 - a` 不行吗？

**参考答案**：`0 - a` 是「二元减法」，按 `Fp16Sub(0, a)` 计算，对 `a = +0.0` 会得到 `+0.0` 而非 `-0.0`，丢失了符号零；而且它依赖一个现成的 `0` 字面量并多走一次加法。一元 `operator-()` 以「翻转符号位」实现，精确对应 IEEE 754 取负语义，能保留 ±0 与 NaN payload。

**练习 2**：在 `kernel_fp16.h` 与 `kernel_bf16.h` 中，`half` 与 `Bf16T` 都只有一个数据成员。说出这个成员的类型与作用。

**参考答案**：分别是 `uint16_t half::val` 与 `uint16_t Bf16T::val`。它直接持有 16 位浮点的原始比特布局，所有运算符都围绕这个比特成员展开，从而保证 CPU 侧的位模式与 NPU 完全一致。

**练习 3**：为什么说 `BF16_EXP_BIAS` 直接取 `FP32_EXP_BIAS`（127）反映了 bf16 的设计初衷？

**参考答案**：bf16 的指数段（8 位）与 fp32 完全相同，尾数段只是 fp32（23 位）的低 16 位被截断。因此 fp32↔bf16 的转换几乎只是「保留高 16 位 / 低位补零」，这正是 bf16「牺牲精度换取与 fp32 相同的数值范围、且转换极廉价」的设计目标。

---

### 4.2 fp8 / fp4 / hif8：8 位与 4 位「传输型」格式

#### 4.2.1 概念说明

随着大模型训练与推理对显存/带宽的极致追求，NPU 引入了比 fp16 更窄的浮点格式：**fp8**（8 位）、**fp4**（4 位）、以及华为定义的 **hif8（HiFloat8，8 位）**。这些格式的命名遵循 `E_xM_y` 约定，表示「指数 x 位 + 尾数 y 位」：

| 类型 | 别名（typedef） | 位宽 | S/E/M 布局 | 典型用途 |
| --- | --- | --- | --- | --- |
| fp8 e4m3 | `fp8_e4m3fn_t` | 8 | 1/4/3 | 前向推理权重/激活（精度优先） |
| fp8 e5m2 | `fp8_e5m2_t` | 8 | 1/5/2 | 反向梯度（范围优先） |
| fp8 e8m0 | `fp8_e8m0_t` | 8 | 0/8/0 | 微缩放（MX）格式的**共享指数** |
| fp4 e2m1 | `fp4x2_e2m1_t` | 4 | 1/2/1 | 极低比特权重 |
| fp4 e1m2 | `fp4x2_e1m2_t` | 4 | 1/1/2 | 极低比特权重 |
| hif8 | `hifloat8_t` | 8 | 非线性编码 | 训练场景的 8 位浮点 |

> 引用：[cpudebug/include/kernel_fp8_e4m3.h:L20-L25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp8_e4m3.h#L20-L25) 与 [cpudebug/include/kernel_fp8_e5m2.h:L20-L25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp8_e5m2.h#L20-L25) 用注释画出 e4m3（1/4/3）与 e5m2（1/5/2）的位布局。

这些类型在 CPU 仿真里扮演的角色与 fp16/bf16 **完全不同**：

- 它们是**传输/存储类型**——只承载比特，不参与标量算术。你不会在源码里写 `fp8_a + fp8_b`，因为 NPU 上这类运算由专门的低精度指令完成。
- 因此它们的接口被刻意设计得很「瘦」：只有「构造、赋值、转回 float/bf16」三类操作，**没有任何算术运算符**。

#### 4.2.2 核心流程

一个 fp8/fp4 值在仿真中的生命周期是「搬运 + 边界处转换」：

```text
[float / bf16 输入]
   │  构造: fp8_e4m3fn_t x(f)   →  FloatToFp8e4m3(f)
   ▼
[int8_t / uint8_t val]  ←—— 仅持有比特，不做任何运算
   │  （可能被打包成 float8_e4m3x2_t，两个 fp8 共用一个 8 位容器）
   ▼
[int8_t / uint8_t val]
   │  还原: (float)x   →  operator float() / ToFloat()
   ▼
[float 输出]  ←—— 在向量内建函数的边界处，比特才被还原成数值参与计算
```

要点：

- **进入**低精度类型时做一次「舍入压缩」，**离开**时做一次「无损拓宽」。压缩/拓宽的精度损失都集中在这两个边界，中间环节纯搬运。
- fp8（e4m3/e5m2/hif8）以 `float` 为中间桥接类型；而 **fp4 以 `bf16` 为桥接类型**（见源码精读），因为 fp4 常与 bf16 配对出现在权重量化路径上。
- `fp8_e8m0` 比较特殊：它没有符号位和尾数，8 位全部是指数，用于微缩放格式里一组 fp8/fp4 数据共享的「缩放因子」，本身不代表一个常规数值。

#### 4.2.3 源码精读

**（1）fp8 类型的「瘦」接口**

以 e4m3 为例，`Fp8e4m3T` 只暴露构造、赋值、两个转换：

> 引用：[cpudebug/include/kernel_fp8_e4m3.h:L26-L45](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp8_e4m3.h#L26-L45) `struct Fp8e4m3T { int8_t val; ... }`，仅声明 `FloatToFp8e4m3`、`operator float()`、`ToFloat()`——没有任何算术运算符。e5m2 结构完全对称（[kernel_fp8_e5m2.h:L26-L45](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp8_e5m2.h#L26-L45)）。

**（2）fp8_e8m0：纯指数的缩放因子**

> 引用：[cpudebug/include/kernel_fp8_e8m0.h:L20-L24](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp8_e8m0.h#L20-L24) 注释明确写出「0 bit SIGN / 8 bit EXP / 0 bit MAN」——它就是一个 8 位指数。

而且它的 `AscendC` 命名空间别名被架构宏限定，只在特定核架构下启用：

> 引用：[cpudebug/include/kernel_fp8_e8m0.h:L45-L49](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp8_e8m0.h#L45-L49) 仅当 `__NPU_ARCH__ == 3510` 或 `5102`（即 ascend950pr_9599）时，才在 `namespace AscendC` 里暴露 `fp8_e8m0_t`。这与 u3-l2 提到的「部分低精度/SIMT 能力仅在 9599 架构启用」一致。

**（3）fp4 以 bf16 为桥接**

fp4 的两个变体（e2m1/e1m2）不从 `float` 构造，而从 `bfloat16::Bf16T` 构造，也只支持转回 `Bf16T`：

> 引用：[cpudebug/include/kernel_fp4_e2m1.h:L42-L60](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp4_e2m1.h#L42-L60) `Fp4e2m1T` 的构造函数接受 `bfloat16::Bf16T`，声明 `BfloatToFp4e2m1` 与 `operator bfloat16::Bf16T()`；这也是它 `#include "kernel_bf16.h"` 的原因（[L18](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp4_e2m1.h#L18)）。e1m2 与之同构（[kernel_fp4_e1m2.h:L42-L60](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp4_e1m2.h#L42-L60)）。

**（4）hif8：非线性的 8 位编码**

hif8（HiFloat8）的位布局不是简单的等宽 S/E/M，而是把 8 位拆成「符号 + 档位 + 指数 + 尾数」的非线性编码，以在 8 位内兼顾训练所需的动态范围与精度：

> 引用：[cpudebug/include/kernel_hif8.h:L20-L35](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_hif8.h#L20-L35) 用一张表描述 hif8 的 subnormal/normal 各档位如何编码数值；其转换函数 `FloatToHif8`/`ToFloat` 实现同样位于闭源库。

**（5）「成对打包」类型**

`kernel_vectorized.h` 把上述低精度类型两两打包，方便按 16 位容器一次搬运两个元素：

> 引用：[cpudebug/include/kernel_vectorized.h:L70-L73](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_vectorized.h#L70-L73) 定义 `half2`、`bhalf2`（两个 half/bf16 打包）。

> 引用：[cpudebug/include/kernel_vectorized.h:L105-L110](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_vectorized.h#L105-L110) 定义 `bfloat16x2_t`、`hifloat8x2_t`、`float8_e4m3x2_t`、`float8_e5m2x2_t`——这些「x2」类型是把两个窄浮点放进一个 16 位寄存器宽度的载体里。

#### 4.2.4 代码实践

**实践目标**：用源码本身梳理「asc-tools 一共仿真了哪些低精度浮点类型」，并验证「传输型类型没有算术运算符」这一结论。

**操作步骤**：

1. 列出所有低精度头文件：
   ```bash
   ls cpudebug/include/kernel_fp*.h cpudebug/include/kernel_hif8.h cpudebug/include/kernel_bf16.h
   ```
2. 统计每个 `struct` 声明了多少个 `operator`：
   ```bash
   grep -c "operator" cpudebug/include/kernel_fp8_e4m3.h \
                   cpudebug/include/kernel_fp4_e2m1.h \
                   cpudebug/include/kernel_hif8.h \
                   cpudebug/include/kernel_fp16.h
   ```
3. 在 `kernel_vectorized.h` 中确认所有「x2」打包类型都来源于前一步列出的基础类型。

**需要观察的现象**：

- fp8/fp4/hif8 头文件里 `operator` 命中数极低（通常只有 `operator=` 与一个转换 `operator T()`），而 `kernel_fp16.h` 命中数很高（几十个）。
- 没有任何 fp8/fp4/hif8 结构体声明 `operator+`/`operator-`/`operator*`。

**预期结果**：用一张表把所有类型及其「是否有算术运算符」归纳出来，你会清楚地看到「计算型 vs 传输型」的分野——这正是 cpudebug 仿真低精度类型的设计准则：**只仿真到「能忠实搬运与边界转换」的程度，真正的低精度运算交给 NPU 指令**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Fp4e2m1T` 的构造函数接受的是 `bfloat16::Bf16T` 而不是 `float`？

**参考答案**：fp4 量化路径常与 bf16 权重配对，以 bf16 为中介可以避免多余的 fp32↔bf16 转换；同时 bf16 的 8 位指数提供了足够动态范围来承载 fp4 的舍入压缩。这也是 `kernel_fp4_e2m1.h` 需要 `#include "kernel_bf16.h"` 的原因。

**练习 2**：`fp8_e8m0_t` 与其它 fp8 格式有何本质不同？

**参考答案**：e8m0 没有符号位和尾数，8 位全部是指数，它不表示一个常规数值，而是微缩放（MX）格式中一组 fp8/fp4 数据**共享的缩放因子（指数）**；并且它的 `AscendC` 命名空间别名只在 `__NPU_ARCH__ == 3510/5102` 下启用。

**练习 3**：如果你在算子里写 `fp8_e4m3fn_t a, b; auto c = a + b;`，会发生什么？为什么？

**参考答案**：编译失败（或触发非预期的隐式转换）。因为 `Fp8e4m3T` 没有重载 `operator+`，它是传输型类型；低精度加法应由向量内建函数在 NPU 指令边界完成，而不是 C++ 标量加法。这也提醒我们：仿真只覆盖「搬运与转换」，不覆盖低精度标量运算。

---

### 4.3 类型转换与舍入：FloatToFp16 精读

#### 4.3.1 概念说明

低精度仿真的可信度，几乎全部押在「类型转换」这一环上——只要 `float → fpN` 与 `fpN → float` 两端都精确，中间的比特搬运就不会引入额外误差。其中最关键、也最容易出错的是**舍入（rounding）**。

cpudebug 默认采用 **就近偶舍入（round-to-nearest-even, RNE）**，即 IEEE 754 的默认舍入模式：当待舍去的位正好等于「一半」时，向最近的**偶数**对齐，避免长期统计偏差。RNE 的判定逻辑被抽象为一个工具函数 `IsRoundOne`，被各种 `XxxToFp16`/`Fp16ToXxx` 复用。

> 引用：[cpudebug/src/acl_stub/kernel_fp16.cpp:L28](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L28) 全局常量 `ROUND_MODE = K_ROUND_TO_NEAREST` 设定默认舍入模式为就近偶舍入。

理解 `FloatToFp16` 的意义在于：它是一切低精度「窄化转换」的范本——bf16、fp8、fp4 的转换函数都遵循类似的「分段 + 舍入」结构。

#### 4.3.2 核心流程

把一个 32 位 `float`（1/8/23）压成 16 位 `fp16`（1/5/10），尾数要从 23 位砍到 10 位，指数要从 8 位（bias 127）重映射到 5 位（bias 15）。`FloatToFp16` 按输入指数 `ef` 分三段处理：

```text
ef（float 的原始指数，8 位无偏移值）
  │
  ├── ef > 0x8F（142 = 127 + 15）        →【上溢】fp16 指数超 5 位上限
  │      eRet = MAX_EXP - 1, mRet = MAX_MAN   （即 ±Inf/NaN 区段）
  │
  ├── ef <= 0x70（112 = 127 - 15）        →【下溢】落入 fp16 非规格化或 0
  │      若 ef >= 0x67（103 = 127 - 24） → 舍入成 fp16 非规格化数
  │      若 ef == 0x66 且尾数非 0         → 截断到最小非规格化数（mRet = 1）
  │      否则                              → 置 0
  │
  └── 其余（0x71..0x8F）                  →【常规】无溢出的正常重映射
         eRet = ef - 0x70
         对砍掉的 13 位尾数做 RNE 舍入
         若舍入导致尾数进位到隐含位，则指数 +1
```

其中指数的两个临界值直接来自两种格式的 bias 差：

\[ \Delta\text{bias} = \text{bias}_{\text{fp32}} - \text{bias}_{\text{fp16}} = 127 - 15 = 112 \;(=0\text{x}70) \]

上溢边界 \(127 + 15 = 142 = 0\text{x}8\text{F}\)，下溢边界 \(127 - 15 = 112 = 0\text{x}70\)，完全自洽。

RNE 舍入的判定由 `IsRoundOne` 完成：它取出「被截断的最高位（truncHigh）」「截断剩余位（truncLeft）」「保留的最低位（lastBit）」，按 RNE 规则决定是否进一：

\[ \text{round\_up} = \text{truncHigh} \land (\text{lastBit} \lor \text{truncLeft}) \]

> 引用：[cpudebug/src/acl_stub/kernel_fp16.cpp:L50-L68](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L50-L68) `IsRoundOne` 用三个掩码分离出 lastBit/truncHigh/truncLeft，并仅在 `K_ROUND_TO_NEAREST` 模式下采用 RNE 判定。

反方向（`fp16 → float`）是「无损拓宽」：把 10 位尾数左移补齐到 23 位、指数重映射回 8 位，不存在舍入。

#### 4.3.3 源码精读

**（1）FloatToFp16 三段式主体**

> 引用：[cpudebug/src/acl_stub/kernel_fp16.cpp:L993-L1051](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L993-L1051) `half::FloatToFp16` 的完整实现：先把 `float` 按位解释成 `uint32_t`，分离 S/E/M，再按 `ef` 落入上溢/下溢/常规三段之一，最后 `Fp16Normalize` + `FP16_CONSTRUCTOR` 重组。

**（2）按位解释 + 掩码分离**

转换的第一步是把 `float` 的 32 位按位重解释为整型，再用掩码取出三段：

> 引用：[cpudebug/src/acl_stub/kernel_fp16.cpp:L1000-L1012](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L1000-L1012) `reinterpret_cast<const uint32_t*>(&fVal)` 取出 32 位比特，随后用 `K_FP32_SIGN_MASK`/`K_FP32_EXP_MASK`/`K_FP32_MAN_MASK` 分别抽出符号、指数、尾数。这是所有浮点格式转换的通用起手式。

**（3）反向拓宽 Fp16ToFloat**

> 引用：[cpudebug/src/acl_stub/kernel_fp16.cpp:L95-L137](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L95-L137) `Fp16ToFloat` 把 5 位指数重映射到 8 位、10 位尾数左移补齐到 23 位，并特判 Inf/NaN（`hfExp == 31`）与零。该函数同时支撑 `operator float()` 与 `ToFloat()`（[L1428](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L1428)、[L1451](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/acl_stub/kernel_fp16.cpp#L1451)）。

**（4）位运算辅助宏**

`FP16_CONSTRUCTOR` 把「符号 + 指数 + 尾数」重新拼回 16 位：

> 引用：[cpudebug/include/kernel_fp16.h:L129-L135](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp16.h#L129-L135) `FP16_CONSTRUCTOR(s, e, m)` 把三段移位并按位或起来；提取方向见 `FP16_EXTRAC_SIGN/EXP/MAN`（[L107-L126](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_fp16.h#L107-L126)）。这套宏构成了 fp16 仿真「拆解—运算—重组」的基础词汇。

#### 4.3.4 代码实践

**实践目标**：用一个具体数值，手工走一遍 `FloatToFp16` 的三段式流程，验证舍入行为。

**操作步骤**：

1. 取 `fVal = 1.0f`。它的 IEEE 754 编码是 `0x3F800000`：S=0、E=0x7F（127）、M=0。
2. 按 `FloatToFp16` 推演：
   - `ef = 0x7F = 127`，落入「常规」段（\(0x71 \le 127 \le 0x8F\)）；
   - `eRet = ef - 0x70 = 127 - 112 = 15`，正好等于 fp16 的 bias，对应 \(2^0\)；
   - 尾数 `mf = 0`，舍入后仍为 0；
   - 重组得到 `FP16_CONSTRUCTOR(0, 15, 0) = 0x3C00`，即 fp16 的 `1.0`。
3. 取 `fVal = 0.1f`（一个无法被二进制精确表示的值），重复推演，关注末尾 13 位尾数的 RNE 舍入方向。
4. （可选）写一段最小 C++ 程序，包含 `kernel_fp16.h`，构造 `half h(0.1f); printf("%04x\n", h.val);`，与本讲推演对照。

**需要观察的现象**：

- `1.0f` 转换后 `val == 0x3C00`，与 IEEE 754 fp16 的 `1.0` 完全一致。
- `0.1f` 的舍入结果取决于被截断尾数的具体比特，应当与标准 fp16 编码 `0x2E66` 一致（待本地验证）。

**预期结果**：你能用 `ef` 的三段判定与 RNE 公式手工预测任意 `float` 的 fp16 编码，从而真正理解「仿真保真度由转换函数决定」。如果第 4 步无法在本机编译（缺少 CANN 工具链），可标注为「待本地验证」，转而直接阅读 `FloatToFp16` 源码完成推演。

#### 4.3.5 小练习与答案

**练习 1**：`FloatToFp16` 里 `0x70` 和 `0x8F` 这两个魔数分别代表什么？

**参考答案**：`0x70 = 112 = 127 - 15`，是 fp32 与 fp16 的 bias 之差，作为「常规段」指数重映射的基准；`0x8F = 142 = 127 + 15`，是 fp16 指数能表达的上限（加上 fp32 的 bias），超过即判定为上溢。

**练习 2**：为什么 `Fp16ToFloat` 不需要像 `FloatToFp16` 那样调用 `IsRoundOne`？

**参考答案**：`fp16 → float` 是把窄格式拓宽到宽格式，10 位尾数左移补齐到 23 位后不会丢失任何信息，属于无损转换，因此不需要舍入；只有「宽 → 窄」需要丢弃低位比特，才涉及 RNE 舍入。

**练习 3**：就近偶舍入（RNE）相比「截断（truncation）」有什么好处？

**参考答案**：截断会引入系统性偏差（总是把数值往零方向拉低），长期累加会偏置结果；RNE 在「正好一半」时向最近偶数对齐，使正负舍入误差在统计上相互抵消，是无偏的，也是 IEEE 754 的默认舍入模式。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「类型普查 + 转换追踪」小任务：

1. **普查**：从 `kernel_vectorized.h` 出发，列出 asc-tools 在 CPU 域仿真的**全部**低精度浮点类型（fp16/bf16/fp8×3/fp4×2/hif8），填出一张表，包含：typedef 别名、位宽、S/E/M 布局、是否含算术运算符、桥接类型（float 还是 bf16）、实现归属（开源 `kernel_fp16.cpp` 还是闭源模型库）。

2. **分类**：在表里用一句话说明每个类型属于「计算型」还是「传输型」，并解释为什么传输型不提供 `operator+`。

3. **转换追踪**：选一个 `float` 值（如 `1.0f`、`0.1f`），沿 `FloatToFp16` 的三段式手工推演它的 fp16 编码；再沿 `Fp16ToFloat` 反推，验证往返是否一致。

4. **运算符核查**：用 `git show c6f35b0` 与 `grep -rn "operator-()" cpudebug/` 确认：本次提交只新增了 `half`/`Bf16T` 的一元 `operator-()` **声明**；在开源 `.cpp` 中找不到它的函数体，`Bf16T` 的全部实现也不在开源树。结合 `Fp16Sub` 的符号位翻转技巧，写出一元 `operator-()` 的「自然实现」（异或 `0x8000`），并说明它为何比 `0 - x` 更符合 IEEE 754 语义。

完成后，你应当能用一句话向他人解释：**cpudebug 如何在 CPU 上「重新发明」一整套 NPU 低精度浮点类型，并保证转换两端精确、行为可复现。**

## 6. 本讲小结

- cpudebug 在 CPU 上「重新发明」了 fp16/bf16/fp8/fp4/hif8 等 NPU 专有类型，根本目的是让孪生调试中的比特布局与 NPU 完全一致。
- 仿真类型分为两类：**计算型（fp16/bf16）**拥有完整算术运算符，可直接参与标量运算；**传输型（fp8/fp4/hif8）**只提供「构造 + 赋值 + 边界转换」，不含算术，真正的低精度运算交给 NPU 指令。
- `half`/`Bf16T` 都用单个 `uint16_t val` 持有比特，`half` 的实现开源于 `kernel_fp16.cpp`，`Bf16T` 的实现位于闭源模型库。
- 仿真的可信度集中在「类型转换」一环：`FloatToFp16` 按「上溢 / 下溢 / 常规」三段处理，并用 `IsRoundOne` 实现就近偶舍入（RNE）；反向 `Fp16ToFloat` 是无损拓宽。
- 最近提交 `c6f35b0` 为 `half`/`Bf16T` 补全了一元 `operator-()`；一元取负应以「翻转符号位」实现，从而精确保留 ±0 与 NaN payload，这是二元减法或 float 往返无法替代的。
- 在开源树中，`half::operator-()` 的函数体与 `Bf16T` 全部方法均不可见——它们要么由闭源库提供，要么暂无调用点；调用前务必用 `nm` 等手段确认符号已定义。

## 7. 下一步学习建议

- **向校验侧延伸**：低精度类型的运算最终经由向量内建函数执行，而这些调用会被 API 校验框架检查。建议接着学习 **u4-1 校验基类与通用检查机制**，看 `api_check` 如何在 CPU 域守护这些类型的正确使用。
- **向生成侧延伸**：`half`/`Bf16T` 的运算符为何「不该手写分发表」、如何与 stub 注册协作，可回顾 **u3-3 Stub 注册与内建函数转义**，理解 `AscendC`/`cceprint`/`npuchk` 三类实现如何与这些类型挂钩。
- **动手验证**：如果你本地有 CANN 工具链，可参照 u1-l4 编译 add 样例，把 `TQue` 的数据类型从默认改成 `half`，用 `nm -C` 观察 `libcpudebug.so` 中 `half::operator-` 系列符号的有无，把本讲「待本地验证」的结论补齐。
