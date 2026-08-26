# 类型系统与公共常量

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `half`、`aclFloat16`、`float16_t`、`bfloat16_t`、`float32_t` 这些 PTO 类型在 CPU 模拟器上分别映射到哪个 C++ 类型，在 NPU 上又来自哪里。
2. 解释 `AICORE`、`PTO_INST`、`PTO_INTERNAL`、`OP_NAME` 这几个编译期宏的作用，以及它们为什么在 CPU 与 NPU 两种编译环境下展开结果不同。
3. 掌握 `constants.hpp` 中「32 字节块 / 256 字节 repeat」这组硬件常量与新增的位宽换算常量 `BIT_TO_BYTE`，并能推导 \( \text{elementsPerRepeat} = 256 / \text{sizeof}(T) \) 这类派生参数。
4. 说出 `MrgSortExecutedNumList`、`TRandomKey` 这类**公共结构体/类型别名**为什么必须定义在 `common` 层，以及它们分别被哪条指令消费。
5. 了解 `kernel_meta.hpp` 如何用一段 `.ascend.meta` 数据向运行时声明「这个内核是 AIC、AIV 还是混合核」。
6. 亲手触发一次 PTO 的编译期类型检查，并准确定位错误信息在源码中的出处。

## 2. 前置知识

本讲不需要任何 NPU 开发经验，但要先建立几个直观印象：

- **header-only 模板库**（承接 [u1-l5](u1-l5-entry-header-and-backends.md)）：PTO 没有独立的库文件需要链接，所有代码以 C++ 头文件形式提供，编译你的内核时，PTO 的实现一起被编进来。后端由宏决定：`__CPU_SIM`（CPU 模拟器）、`__CCE_AICORE__`（NPU 真机）、`__COSTMODEL`（性能估算）。
- **编译期检查**：C++ 的 `static_assert(条件, "消息")` 在编译时求值，条件为假则直接报编译错误。PTO 把大量「这个指令支持哪些数据类型」「tile 必须行主序」的约束写成 `static_assert`，让非法用法根本无法通过编译——这比运行时崩溃友好得多。
- **浮点格式速览**：
  - **FP16（half）**：16 位，1 符号 + 5 指数 + 10 尾数，动态范围小、精度较高；
  - **BF16（bfloat16）**：16 位，1 符号 + 8 指数 + 7 尾数，指数位与 FP32 相同，动态范围大、精度低，深度学习常用；
  - **FP8/FP4**：8 位 / 4 位的极低精度格式（如 e4m3、e2m1），配合缩放因子用于大模型推理加速。
  - CPU 上的 `_Float16` 是编译器原生支持的 IEEE FP16 类型（需要较新的 GCC/Clang，具体版本要求见 `docs/getting-started.md`）。
- **NPU 片上存储层级**（承接 [u1-l4](u1-l4-first-kernel-tadd.md)）：数据从 GM（全局内存）搬入 UB（Vector 核的统一缓冲），计算后写回。UB/L1/L0 的容量是有限的，本讲会看到这些容量的常量定义在哪里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/common/type.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp) | 类型别名（half/bfloat16_t 等）、编译期宏（AICORE/PTO_INST）、断言宏（PTO_STATIC_ASSERT/PTO_CPU_ASSERT）、指令行为枚举（TileType/BLayout/RoundMode 等）、公共结构体与类型别名（MrgSortExecutedNumList/TRandomKey） |
| [include/pto/common/constants.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp) | 硬件几何常量（32 字节块、256 字节 repeat、分形行数、BIT_TO_BYTE 位宽换算等）、PadValue 体系、掩码步进辅助函数 |
| [include/pto/common/kernel_meta.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/kernel_meta.hpp) | SYNCALL 混合核场景下，向运行时声明核类型（AIC/AIV/混合）的 ELF 元信息 |

本讲还会**顺带引用**（只取片段，不深入）：

| 文件 | 引用原因 |
| --- | --- |
| [include/pto/common/buffer_limits.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/buffer_limits.hpp) | UB/L1/L0 容量常量的真正出处（详细规划在 u2-l4 讲） |
| [include/pto/cpu/MXTypes.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/MXTypes.hpp) | CPU 模拟器上的 FP8/FP4 低精度类型定义 |
| [include/pto/npu/a2a3/TAdd.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp) 与 [include/pto/npu/a5/TAdd.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAdd.hpp) | 类型白名单 `static_assert` 的真实出处（本讲实践的主角） |
| [include/pto/cpu/TAdd.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp) | 对照组：CPU 路径为什么查不出类型违规 |
| [include/pto/common/pto_instr.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp) | TADD/TMRGSORT/TRANDOM 公共声明，宏 `PTO_INST` 与公共结构体的使用现场 |
| [include/pto/cpu/TMrgSort.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TMrgSort.hpp) 与 [include/pto/npu/a2a3/TRem.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp) | 公共结构体 `MrgSortExecutedNumList` 与常量 `BIT_TO_BYTE` 的真实消费现场 |
| [docs/isa/TMRGSORT.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TMRGSORT.md) | `MrgSortExecutedNumList` 的语义文档 |

**本版本更新提示**（相对上一版讲义，`0dbecbe → be5ccb76` 的 diff）：`type.hpp` 新增了公共结构体 `MrgSortExecutedNumList` 并删除了多余的 `#include <type_traits>`；`constants.hpp` 新增位宽换算常量 `BIT_TO_BYTE`；`a5/TAdd.hpp` 仅删除了一行重复的 include（白名单本体未变，行号整体上移 1 行）。

## 4. 核心概念与源码讲解

### 4.1 数据类型别名：一份类型名，多套后端

#### 4.1.1 概念说明

PTO 的目标是「一份内核代码，多个 Ascend 后端都能编译」。第一个拦路虎就是类型名：

- 在 NPU 上，`half` 是 CCE 编译器的**内建类型**，CPU 的 g++/clang 根本不认识它；
- 反过来，CPU 上模拟 BF16 需要依赖工具链版本。

所以 PTO 的策略是：**代码里统一写 `half`、`bfloat16_t`、`float32_t` 这些名字，由 `type.hpp` 在 CPU/CostModel 编译分支下给出等价定义**；在 NPU 编译分支下则什么都不定义，直接使用 CCE 内建类型。这样上层内核代码完全不用关心自己在哪个后端。

#### 4.1.2 核心流程

```text
编写内核：使用 pto::Tile<TileType::Vec, half, 16, 256>
        │
        ├─ NPU 编译（未定义 __CPU_SIM/__COSTMODEL）
        │     └─ half = CCE 编译器内建类型，type.hpp 不做任何定义
        │
        └─ CPU/CostModel 编译（定义了 __CPU_SIM 或 __COSTMODEL）
              └─ type.hpp 末尾给出 typedef：
                    half        → _Float16
                    aclFloat16  → _Float16
                    float16_t   → half（即 _Float16）
                    float32_t   → float
                    bfloat16_t  → 三档策略（见 4.1.3）
```

`bfloat16_t` 的三档策略值得单独记住：

1. 工具链支持 C++23 `<stdfloat>` 且定义了 `__STDCPP_BFLOAT16_T__` → 用真 `std::bfloat16_t`（位精确）；
2. 用户显式定义了 `PTO_CPU_SIM_ENABLE_BF16` 但工具链不支持 → 直接 `#error` 报错（宁可大声失败，不静默降级）；
3. 默认 → 用 `_Float16` 占位（数值语义够用，但不是位精确的 BF16）。

#### 4.1.3 源码精读

**类型别名的定义处**——整个「跨后端类型」机制就浓缩在这几行：

- [include/pto/common/type.hpp:L447-L470](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L447-L470)：在 `__CPU_SIM || __COSTMODEL` 下定义 `half`/`aclFloat16`/`float16_t`/`float32_t`，以及 `bfloat16_t` 的三档选择逻辑。注意第 452 行起的注释：要求 clang ≥ 15、gcc ≥ 14 才能自动启用原生 BF16。

**4 比特打包类型 `int4b_t`**——两个元素打包进一个字节：

- [include/pto/common/type.hpp:L108-L118](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L108-L118)：用 `uint8_t` 做存储，构造时截取低 4 位，转回 `int8_t` 时按符号位扩展。它特意放在 `namespace pto` 里，避免和 AscendC 全局作用域的 `int4b_t` 冲突（见第 100-104 行注释）。

**CPU 模拟器上的 MX 低精度类型**（FP8/FP4，主要服务 A5 的 MX 量化路径）：

- [include/pto/cpu/MXTypes.hpp:L232-L236](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/MXTypes.hpp#L232-L236)：`float4_e2m1x2_t`、`float8_e4m3_t` 等是「指数/尾数位数 + bias」作模板参数的软实现。`constants.hpp` 中 A5 分支的 PadValue 特化引用了这些类型名（[include/pto/common/constants.hpp:L452-L472](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp#L452-L472)）。

**类型系统的另一面：行为枚举**。`type.hpp` 中部还定义了大量 `enum class`，它们虽然不是「数据类型」，但同属类型系统：`TileType`（Vec/Mat/Left/Right/Acc…，决定 tile 落在哪块片上缓冲，见 [include/pto/common/type.hpp:L121-L132](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L121-L132)）、`BLayout`/`SLayout`（行主/列主，L134-L143）、`RoundMode`（舍入模式，L249-L257）、`Layout`（ND/DN/NZ 等全局布局，L163-L187）。这些在 [u2-l3](u2-l3-tile-type-deep-dive.md) 会展开。

#### 4.1.4 代码实践：整理类型速查表

**实践目标**：产出一张「PTO 类型 → C++ 类型 → 支持的指令类别」速查表，作为后续查阅的常备工具。

**操作步骤**：

1. 通读 [include/pto/common/type.hpp:L447-L470](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L447-L470) 与 [include/pto/cpu/MXTypes.hpp:L232-L236](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/MXTypes.hpp#L232-L236)。
2. 用下面的表格模板填写（前两列照抄源码；第三列以 TADD 为锚点，查 [docs/isa/TADD.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TADD.md) 的 Constraints 一节，完整指令覆盖面查 `include/README.md` 状态表）：

| PTO 类型 | CPU 模拟器上的 C++ 类型 | 以 TADD 为例的支持情况（A2A3 / A5） |
| --- | --- | --- |
| `half` | `_Float16` | 两者都支持 |
| `float16_t` | `half` 的别名（`_Float16`） | 两者都支持 |
| `aclFloat16` | `_Float16` | —（宿主侧数据类型，见下） |
| `float32_t` / `float` | `float` | 两者都支持 |
| `bfloat16_t` | `std::bfloat16_t`（C++23）或 `_Float16` 占位 | 仅 A5 支持 TADD |
| `int16_t` / `int32_t` | 原生整型 | 两者都支持 |
| `int8_t` / `uint8_t` / `int64_t` / `uint64_t` | 原生整型 | 仅 A5 支持 TADD |
| `pto::int4b_t` | 打包进 `uint8_t`（2 个/字节） | 配合 vconv 类指令使用 |
| `float4_*` / `float8_*` / `hifloat8_t` | `cpu/MXTypes.hpp` 软实现 | A5 MX 量化路径 |

3. 关于 `aclFloat16`：它在 type.hpp 中与 `half` 同为 `_Float16`，这个名字主要用于与 CANN 宿主侧（acl）数据类型对齐；在本仓库内核代码里更常用的是 `half` / `float16_t`。

**需要观察的现象**：填写第三列时，你会发现 A5 的类型白名单明显比 A2A3 宽（A5 多出 bf16、8 位整型、64 位整型）——这正呼应 u1-l1 讲过的「CPU 与 NPU 列并非子集关系」，不同代际硬件的向量单元能力不同。A5 白名单里的 `int64_t`/`uint64_t` 正是 [u4-l7](u4-l7-a5-int64-emulation.md) 要讲的 64 位寄存器对仿真路径。

**预期结果**：一张 10 行左右的速查表。注意「支持哪些指令」必须逐指令查文档/状态表，不能由类型反推。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bfloat16_t` 在默认 CPU 构建里可能是 `_Float16` 占位？这会带来什么风险？

**答案**：`_Float16` 是 FP16（10 位尾数），BF16 只有 7 位尾数但 8 位指数。占位意味着 CPU 模拟器按 FP16 的精度与动态范围做数值模拟，大多数算子的「数值正确性验证」仍然成立，但涉及 BF16 特有行为（如溢出边界、位级编码）的测试不可靠。所以 type.hpp 用 `PTO_CPU_SIM_ENABLE_BF16` 提供「严格请求」：在不支持的工具链上直接编译失败，而不是静默降级（[type.hpp:L460-L461](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L460-L461)）。

**练习 2**：`int4b_t` 为什么定义在 `namespace pto` 内，而不是像 `half` 那样放在全局作用域？

**答案**：源码注释（[type.hpp:L100-L104](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L100-L104)）写明：某些 AscendC 内部头文件会在全局作用域暴露 `int4b_t` 别名，全局定义会冲突；放进 `pto` 命名空间后，PTO 代码中未限定的 `int4b_t` 解析到 `pto::int4b_t`，互不干扰。

**练习 3**：`float16_t` 和 `half` 是什么关系？

**答案**：CPU 下 `typedef half float16_t;`（[type.hpp:L450](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L450)），是完全相同的类型，只是两个习惯叫法；所以 a2a3 的 TADD 白名单里两者都列出（`std::is_same` 对同一类型恒真，列两次只是可读性）。

### 4.2 编译期宏：AICORE、PTO_INST 与断言体系

#### 4.2.1 概念说明

u1-l5 讲过「后端由宏决定」。本模块看**更细一粒度**的宏：它们不选择后端，而是让**同一个函数签名**在两种编译器下都合法、都高效：

- `AICORE`：NPU 下展开为 `[aicore]`——CCE 编译器的地址空间标注，表示「这个函数在 AI Core 上执行、可被设备侧调用」；CPU 下展开为空。
- `PTO_INST` / `PTO_INTERNAL`：给**公共指令接口**和**内部实现**分别贴的修饰组合，都强制内联（`always_inline`），区别是 `PTO_INST` 额外带 `visibility("default")` 便于跨编译单元导出。
- `OP_NAME` / `OP_TYPE`：NPU 下给函数贴 `vf_name`/`vf_kind` 属性，供 CCE 向量化函数框架识别；CPU 下为空。
- `PTO_STATIC_ASSERT` / `PTO_CPU_ASSERT`：把「诊断前缀 + 违反的条件字符串 + 排查文档线索」统一进断言消息，这是 PTO 错误信息可检索的关键设计。

#### 4.2.2 核心流程

```text
一段 PTO 指令声明的生命周期（以 TADD 为例）：

pto_instr.hpp 中：PTO_INST RecordEvent TADD(...)   ← 公共接口贴 PTO_INST
        │
        ├─ NPU 编译：PTO_INST = [aicore] inline always_inline visibility(default)
        │            指令实现文件里再贴 PTO_INTERNAL（不带 visibility）
        │
        └─ CPU 编译：PTO_INST = inline always_inline visibility(default)
                     （[aicore] 消失，g++ 能直接编译）

类型/约束违规时：
        static_assert 路径（编译期）→ PTO_STATIC_ASSERT，消息形如
              "[PTO][SA] ... Condition: xxx. Hint: see docs/coding/debug.md ..."
        运行期路径（仅 CPU/CostModel）→ PTO_CPU_ASSERT，stderr 打印 + abort
```

#### 4.2.3 源码精读

**AICORE 与函数修饰宏**：

- [include/pto/common/type.hpp:L13-L23](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L13-L23)：`#if !defined(__CPU_SIM) && !defined(__COSTMODEL)` 时 `AICORE` 才是 `[aicore]`；`PTO_INST`/`PTO_INTERNAL` 的定义紧随其后。
- [include/pto/common/type.hpp:L25-L31](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L25-L31)：`OP_NAME`/`OP_TYPE` 在 CPU/CostModel 下退化为空宏。

**使用现场**——TADD 的公共声明就顶着 `PTO_INST`：

- [include/pto/common/pto_instr.hpp:L174-L180](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L174-L180)：`PTO_INST RecordEvent TADD(TileDataDst& dst, ...)`，函数体只做「等待事件 → 转发到后端实现」。

**统一断言宏**：

- [include/pto/common/type.hpp:L48-L64](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L48-L64)：`PTO_STATIC_ASSERT` 用宏重载支持「带/不带自定义消息」两种形式，消息里自动拼入条件原文和 `__FILE__:__LINE__`。
- [include/pto/common/type.hpp:L66-L96](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L66-L96)：`PTO_CPU_ASSERT` 只在 CPU/CostModel 下真实检查并 `abort`，其他后端退化为 `((void)0)`。
- 另有一个更老、更轻的 `PTO_ASSERT`（运行期，仅在定义 `_DEBUG` 时生效）：[include/pto/common/debug.h:L32-L36](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/debug.h#L32-L36)。

**本版本的 include 清理**：diff 显示 type.hpp 中部原来夹在 `int4b_t` 与 `TileType` 之间的一行 `#include <type_traits>` 被删除——type.hpp 自身并不使用它，真正用到 `std::is_same` 的是各后端头文件，它们各自包含所需标准头（例如 [include/pto/cpu/TMrgSort.hpp:L14-L18](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TMrgSort.hpp#L14-L18) 就自带 `#include <type_traits>`）。这提醒我们：**不要依赖传递包含**，自己用到什么就包含什么。

#### 4.2.4 代码实践：触发 TADD 的编译期类型检查

**实践目标**：验证「TADD 对 `int8_t` 在 A2A3 后端会触发编译期报错」，并解释错误信息的确切出处。

**背景事实（先读源码再动手）**：

- 类型白名单的真实出处是 A2A3 后端的 `TAddCheck`：[include/pto/npu/a2a3/TAdd.hpp:L59-L69](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L59-L69)——第 62-66 行的 `static_assert` 只放行 `int32_t/int/int16_t/half/float16_t/float/float32_t`，违规时报 `"Fix: TADD has invalid data type."`。
- A5 后端的白名单更宽，**明确包含 `int8_t`**：[include/pto/npu/a5/TAdd.hpp:L59-L64](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAdd.hpp#L59-L64)。
- CPU 后端的 `TADD_IMPL` **没有任何 dtype 检查**（[include/pto/cpu/TAdd.hpp:L63-L75](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L63-L75)），它只核对三个 tile 的有效区域一致，然后直接跑 `+` 循环。

**操作步骤**：

1. **第一步（对照组）**：写一个 CPU 模拟器小程序，故意用 `int8_t` 跑 TADD（示例代码）：

   ```cpp
   // 示例代码：check_int8_tadd.cpp —— 在 CPU 模拟器上 int8_t 的 TADD 能否编译？
   // 写法逐行对照 tests/cpu/st/testcase/tadd/tadd_kernel.cpp L22-L38（仅去掉搬运与事件）
   #include <pto/pto-inst.hpp>
   using namespace pto;

   void Int8Tadd()
   {
       using TileT = Tile<TileType::Vec, int8_t, 4, 32, BLayout::RowMajor, -1, -1>;
       TileT src0(4, 32), src1(4, 32), dst(4, 32);
       TASSIGN(src0, 0x0);   // 绑定 UB 偏移（CPU 模拟器下只是记录偏移）
       TASSIGN(src1, 0x400);
       TASSIGN(dst, 0x800);
       TADD(dst, src0, src1);
   }
   int main() { return 0; }
   ```

   编译方式参考（与 u1-l2 的 CPU 构建一致，需要较新的 GCC/Clang）：`g++ -std=c++20 -D__CPU_SIM -Iinclude check_int8_tadd.cpp -o check_int8`。

2. **第二步（复现断言）**：由于 A2A3 头文件里的 `__ubuf__`、`vadd` 等只能在 CCE 编译器下编译，我们在 CPU 上**原样复刻**那条 `static_assert` 的条件来观察错误形态（示例代码）：

   ```cpp
   // 示例代码：repro_assert.cpp —— 逐字复刻 a2a3/TAdd.hpp L62-L66 的白名单断言
   #include <type_traits>
   typedef _Float16 half;        // 与 type.hpp L448 的 CPU 定义一致
   typedef half float16_t;       // 与 type.hpp L450 一致
   typedef float float32_t;      // 与 type.hpp L451 一致

   template <typename T>
   void ReproTAddCheck()
   {
       static_assert(
           std::is_same<T, int32_t>::value || std::is_same<T, int>::value || std::is_same<T, int16_t>::value ||
               std::is_same<T, half>::value || std::is_same<T, float16_t>::value || std::is_same<T, float>::value ||
               std::is_same<T, float32_t>::value,
           "Fix: TADD has invalid data type.");
   }
   int main()
   {
       ReproTAddCheck<int8_t>(); // 触发编译错误
       return 0;
   }
   ```

   编译：`g++ -std=c++20 repro_assert.cpp`。

**需要观察的现象**：

- 第一步：CPU 模拟器上 `int8_t` 的 TADD **能够编译通过**（CPU 路径无 dtype 检查）；运行行为待本地验证。
- 第二步：编译器输出 `error: static assertion failed: Fix: TADD has invalid data type.`，并把错误定位到 `ReproTAddCheck<int8_t>` 的实例化行。

**预期结果**：由此得出本讲最重要的结论之一——**dtype 合法性检查在 NPU 后端的 Check 函数里，CPU 模拟器不拦截**。这意味着「CPU 上跑通」不等于「所有后端合法」；写完内核后应对照 [docs/isa/TADD.md:L46-L56](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TADD.md#L46-L56) 的 Constraints（文档明确列出 A2A3 与 A5 两套白名单）核对类型。真正上板（A2A3）编译时，同样的 `int8_t` 内核会由 [a2a3/TAdd.hpp:L62-L66](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L62-L66) 拦下——错误信息出处即此行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `OP_NAME`/`OP_TYPE` 在 CPU 下必须展开为空？

**答案**：它们展开成 `__attribute__((vf_name(...)))`，这是 CCE 编译器私有的向量化函数标注属性，g++/clang 不认识会直接报编译错误（[type.hpp:L25-L31](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L25-L31)）。CPU 下这些元数据没有任何消费者，置空即可。

**练习 2**：`PTO_STATIC_ASSERT` 相比裸写 `static_assert` 好在哪里？

**答案**：它自动把「`[PTO][SA]` 前缀 + 被违反的条件原文（`#cond` 字符串化）+ 文件名与行号 + 指向 docs/coding/debug.md 的提示」拼进诊断消息（[type.hpp:L51-L61](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L51-L61)）。用户拿到报错就能直接拿条件字符串去调试文档里检索，不必猜测是哪条约束挂了。

**练习 3**：`PTO_ASSERT`（debug.h）和 `PTO_CPU_ASSERT`（type.hpp）有什么区别？

**答案**：两者都是运行期检查，但开关不同：`PTO_ASSERT` 只在定义 `_DEBUG` 时生效，用于内核作者调试（如 [cpu/TAdd.hpp:L68-L73](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L68-L73) 检查有效区域一致）；`PTO_CPU_ASSERT` 在 CPU/CostModel 下**始终启用**，打印后 `abort`，用于模拟器必须暴露的非法状态。

### 4.3 容量常量：硬件几何的「度量衡」

#### 4.3.1 概念说明

NPU 的向量指令不是「一个元素一个元素」执行的，它以固定的物理粒度搬运和计算：

- **32 字节块（block）**：向量部件访存的最小对齐单位，即 `BLOCK_BYTE_SIZE = 32`——这正是 u1-l4 里「tile 地址 32 字节对齐」的来源；
- **256 字节 repeat（重复）**：一条向量内置指令一次处理的字节数，即 `REPEAT_BYTE = 256`，一个 repeat 恰好含 \( 256 / 32 = 8 \) 个块；
- **16 元素 / 512 字节**：`BLOCK_LEN = 16` 是 fp16 下一个块的元素数，`CUBE_BLOCK_SIZE = 512` 是 Cube 部件的数据块字节数；
- **8 位/字节**：本版本新增的 `BIT_TO_BYTE = 8`，把「位」换算成「字节」——向量掩码（mask）本质是**按位的比特串**，计算一段数据需要多少掩码字节时就要除以它；
- **分形行数**：`FRACTAL_NZ_ROW = FRACTAL_ZZ_ROW = 16`，Cube 矩阵输入要按 16 行的分形块摆放（u4-l5 详讲）。

而**片上缓冲总容量**（UB 192KB/256KB、L1 512KB 等）不在这里，在 `buffer_limits.hpp`——本讲先建立「两类常量、两个文件」的地图，容量规划留到 [u2-l4](u2-l4-tassign-and-on-chip-memory.md)。

#### 4.3.2 核心流程

一条向量指令的派生参数全靠这些常量推导。以 TADD 为例：

\[ \text{elementsPerRepeat} = \frac{\text{REPEAT\_BYTE}}{\text{sizeof}(T)} = \frac{256}{\text{sizeof}(T)} \]

\[ \text{blockSizeElem} = \frac{\text{BLOCK\_BYTE\_SIZE}}{\text{sizeof}(T)} = \frac{32}{\text{sizeof}(T)} \]

对 fp16（2 字节）：一个 repeat 处理 128 个元素、一个块 16 个元素；对 float（4 字节）：repeat 64 个元素、块 8 个元素。dtype 越窄，单条指令吞吐越大——这就是低精度加速的第一性原理。

`BIT_TO_BYTE` 的用武之地则是**位 ↔ 字节**换算。向量比较/掩码类指令产出的 mask 是比特串：若一行数据跨 `dstRowStride` 位，覆盖它的掩码字节数就是

\[ \text{maskBytes} = \left\lceil \frac{\text{dstRowStride}}{\text{BIT\_TO\_BYTE}} \right\rceil \]

另外两个值得注意的常量：

- `TMP_UB_SIZE = 8KB` / `TMP_UB_OFFSET = 184KB`：PTO 实现**自己在 UB 尾部预留的一小块临时区**（供某些指令的中间数据使用），所以用户手工规划 UB 时要避开这段；
- `MASK_LEN = 64`：向量掩码的位宽（64 个元素一组）。

#### 4.3.3 源码精读

**核心常量定义**：

- [include/pto/common/constants.hpp:L20-L42](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp#L20-L42)：`REPEAT_BYTE`、`BLOCK_MAX_PER_REPEAT`（注释直接写明 `256 / 32 = 8`）、`REPEAT_MAX = 255`、`BLOCK_BYTE_SIZE = 32`、`TMP_UB_SIZE/TMP_UB_OFFSET`、`MASK_LEN`、`BLOCK_LEN = 16`、`CUBE_BLOCK_SIZE = 512`、`C0_SIZE_BYTE`、分形与 MX 常量，以及本版本新增的 `BIT_TO_BYTE = 8`（第 42 行）。
- [include/pto/common/type.hpp:L432-L439](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L432-L439)：`GlobalTensorDim` 命名空间——PTO 的全局张量固定五维（`DIM_0..DIM_4`，`TOTAL_DIM = 5`），这是 u2-l2 讲 Shape 时的几何基础。

**常量如何被真实指令消费**——A2A3 的 TADD 实现：

- [include/pto/npu/a2a3/TAdd.hpp:L83-L93](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L83-L93)：`TADD_IMPL` 用 `blockSizeElem = BLOCK_BYTE_SIZE / sizeof(T)`、`elementsPerRepeat = REPEAT_BYTE / sizeof(T)` 现场推导参数，再传给 `TAdd` 模板。
- [include/pto/npu/a2a3/TAdd.hpp:L20-L31](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L20-L31)：`AddOp` 封装 CCE 内置指令 `vadd(dst, src0, src1, repeats, 1, 1, 1, 8, 8, 8)`——`repeats` 是 `uint8_t`（上限即 `REPEAT_MAX = 255`），三个 `8` 是 repeat 步距（单位为 32 字节块），恰好等于 `BLOCK_MAX_PER_REPEAT`；这组参数如何按 tile 形状拆分，在 u4-l3 结合 `TBinOp` 展开。

**新常量 `BIT_TO_BYTE` 的消费现场**——本版本优化过的 a2a3 TRem（取余/按位掩码指令）用它把行跨度的位数换算成掩码字节数：

- [include/pto/npu/a2a3/TRem.hpp:L211-L213](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp#L211-L213)：`maskBytes = CeilDivision(dstRowStride, BIT_TO_BYTE)` 得掩码字节数，再对齐到块、换算回掩码元素数（`maskFloats`），最后把 `tmpRequiredCols = RowStride + maskFloats + BIT_TO_BYTE` 算进临时 tile 的列需求——掩码本身也要占 UB 空间。TRem 的掩码模式细节见 [u4-l3](u4-l3-npu-a2a3-backend.md)。

**PadValue 体系（类型查表的典型样例）**：`constants.hpp` 的后半部分是「padding 填充值」的类型映射表——主模板对未支持的 dtype 直接用 `static_assert(sizeof(DType) < 0, ...)` 报错（[constants.hpp:L282-L285](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp#L282-L285)），再为每种类型 × 每种标准 PadVal 做特化（如 fp16 的 Min/Max，L366-L380）；`GetPadValue()` 统一取值（[constants.hpp:L477-L499](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp#L477-L499)）。它服务于 TLOAD 的边界填充，本讲只需记住这个模式：**「非法组合在编译期爆炸的穷举查表」在 PTO 里随处可见**。

**容量常量的真正出处**（本讲只指路）：

- [include/pto/common/buffer_limits.hpp:L31-L44](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/buffer_limits.hpp#L31-L44)：UB 容量按 `PTO_NPU_ARCH_*` 宏分档（A5 256KB、A2A3 192KB、Kirin9030 128KB），未知架构直接 `#error`。L1/L0A/L0B/L0C 同文件往下。

#### 4.3.4 代码实践：手推一个 fp16 TADD 的指令参数

**实践目标**：用 `constants.hpp` 的常量，手工推导「16×256 的 fp16 tile 做一次 TADD」的底层指令参数与一行 fp32 数据的掩码字节数，把抽象常量和真实指令联系起来。

**操作步骤**：

1. 确认 dtype：`half` 占 2 字节（CPU 下 `_Float16`，`sizeof == 2`）。
2. 推导三个数：
   - \( \text{elementsPerRepeat} = 256 / 2 = 128 \) 个元素；
   - \( \text{blockSizeElem} = 32 / 2 = 16 \) 个元素；
   - 一行 256 个 fp16 元素 = 512 字节 = 2 个 repeat。
3. 用一个小程序把推导固化成编译期检查（示例代码）：

   ```cpp
   // 示例代码：derived_consts.cpp
   #include <type_traits>
   typedef _Float16 half;

   constexpr int REPEAT_BYTE = 256;             // 与 constants.hpp L20 一致
   constexpr int BLOCK_BYTE_SIZE = 32;          // 与 constants.hpp L23 一致
   constexpr unsigned BLOCK_MAX_PER_REPEAT = 8; // 256 / 32 = 8，与 L21 一致
   constexpr int REPEAT_MAX = 255;              // 与 L22 一致
   constexpr int BIT_TO_BYTE = 8;               // 与 L42 一致（本版本新增）

   static_assert(REPEAT_BYTE == int(BLOCK_MAX_PER_REPEAT) * BLOCK_BYTE_SIZE, "repeat 必须由整数个块组成");
   static_assert(REPEAT_BYTE / sizeof(half) == 128, "fp16 每 repeat 128 个元素");
   static_assert(BLOCK_BYTE_SIZE / sizeof(half) == 16, "fp16 每 block 16 个元素");
   // 掩码按元素个数计位：64 个元素 = 64 位 = 8 字节（与 dtype 宽度无关）
   static_assert(64 * CHAR_BIT / 8 == 64 * BIT_TO_BYTE, "位/字节换算自洽");
   static_assert(64 / BIT_TO_BYTE == 8, "64 个元素的位掩码需 8 字节");
   int main() { return 0; }
   ```

   编译运行：`g++ -std=c++20 derived_consts.cpp && ./a.out`（`CHAR_BIT` 来自 `<climits>`，可自行加 `#include <climits>`）。

**需要观察的现象**：四条 `static_assert` 全部通过、程序静默退出（返回 0）；试着把 `sizeof(half)` 换成 `sizeof(float)` 再编译，观察断言失败形态。

**预期结果**：`REPEAT_BYTE == BLOCK_MAX_PER_REPEAT × BLOCK_BYTE_SIZE` 成立，说明「repeat = 8 个 32 字节块」是自洽的度量衡；换 `float` 后两条元素数断言失败（应为 64 和 8），报错信息直接印出条件原文。掩码断言说明：掩码按**元素个数**计位（64 个元素 = 64 位 = 8 字节），与数据 dtype 宽度无关——这也是 `BIT_TO_BYTE` 存在的意义：位串长度必须再除以 8 才是占用的 UB 字节数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `vadd` 的 `repeats` 参数是 `uint8_t`？

**答案**：硬件 repeat 计数字段就是 8 位，上限 255，对应 `REPEAT_MAX = 255`（[constants.hpp:L22](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp#L22)）。因此单条内置指令最多覆盖 \( 255 \times 256 \approx 63\text{KB} \)，更大的 tile 需要拆成多条 repeat——拆分逻辑由后端实现层（u4-l3 的 TBinOp）完成。

**练习 2**：`TMP_UB_SIZE`/`TMP_UB_OFFSET` 为什么与你有关？

**答案**：PTO 指令实现会在 UB 偏移 184KB 处保留 8KB 作为内部临时区（[constants.hpp:L29-L30](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp#L29-L30)）。手工 TASSIGN 规划 UB 时如果覆盖这段，某些需要临时空间的指令（如 TRem 要在临时 tile 里摆掩码）会破坏你的数据。

**练习 3**：fp16 一个 32 字节块正好 16 个元素，这和 `BLOCK_LEN = 16` 有什么关系？

**答案**：`BLOCK_LEN = 16` 即以 fp16 为「基准 dtype」时一个块的元素数（[constants.hpp:L32](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp#L32)）。所有「按 16 对齐」的约束（如规约指令的列对齐、分形 16 行）本质上都来自 32 字节块在 fp16 下的元素粒度。

### 4.4 公共结构体与类型别名：跨后端共享的「数据包装」

#### 4.4.1 概念说明

有一类东西既不是标量常量、也不是指令行为枚举，而是**指令签名里出现的普通 C++ 聚合类型**：公共声明（`pto_instr.hpp`）要用它做参数类型，cpu/a2a3/a5/costmodel 四套后端实现都要读写它的成员。按照 PTO「**声明在 common、实现按后端归位**」的分层（u1-l3 的仓库地图），它们的定义**必须**放在 common 层——任何一个后端头文件里定义都会造成包含顺序依赖。

本版本（`0dbecbe → be5ccb76`）的 diff 恰好提供了一个活教材：`MrgSortExecutedNumList` 的定义被补进了 type.hpp。在旧 HEAD 的 include 树里甚至找不到它的任何定义或前置声明，而公共签名与各后端实现却都在引用它并访问其成员——这个「悬空引用」随本版本 "Restore public header and CPU ST compilation" 的修复一并落位。

两个代表成员：

- **`MrgSortExecutedNumList`**：TMRGSORT（多路归并排序）指令的**输出参数**。归并多个**变长**已排序序列时，硬件无法预知每个序列多长，于是采用「耗尽即暂停 + 上报进度」的协议：当模板参数 `exhausted = true` 且任一输入序列耗尽时，指令暂停，并通过这个结构体上报**每个输入 list 实际已处理的数量**，用户据此推进各 list 的读指针、装填下一批数据，再次调用。
- **`TRandomKey` / `TRandomCounter`**：TRANDOM 指令（随机数生成）的种子与计数器，是定长 `uint32_t` 数组的类型别名（key 2 个字、counter 4 个字），随调用被原地更新，保证同一序列可续播。

#### 4.4.2 核心流程

以「4 个变长 list 归并排序」为例：

```text
用户：准备 src0..src3（各自装入本批已排序数据）+ tmp（临时 tile）
        │
        ▼
TMRGSORT<..., exhausted=true>(dst, executedNumList, tmp, src0..src3)
        │
        ├─ 归并输出到 dst，直到某个 list 的数据耗尽
        │
        ▼
executedNumList.mrgSortList0..3 = 各 list 本轮实际消费的数量
        │
        ▼
用户：按上报值推进每个 list 的读指针，装入新数据，再次调用
      （循环直至所有序列归并完毕）
```

要点：这是一个**生产者-消费者式的续传协议**——指令与用户代码之间通过结构体交换进度状态，而不是把「序列长度」作为指令参数一次性传入。tile 的 `validRows/validCols`（u2-l3）描述的是**单批**数据量，跨批的进度由 `MrgSortExecutedNumList` 承载。

#### 4.4.3 源码精读

**结构体定义（本版本新增落位）**：

- [include/pto/common/type.hpp:L425-L430](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L425-L430)：`MrgSortExecutedNumList` 就是 4 个 `uint16_t`（`mrgSortList0..3`），紧挨着 `Coalesce` 枚举（L423）之后、`GlobalTensorDim` 命名空间之前，位于 `namespace pto` 内——与 `int4b_t` 同样的命名空间策略。

**公共签名侧的消费**——TMRGSORT 的三个重载（2/3/4 个 list）都以它为第二参数：

- [include/pto/common/pto_instr.hpp:L869-L906](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L869-L906)：`PTO_INST RecordEvent TMRGSORT(DstTileData& dst, MrgSortExecutedNumList& executedNumList, TmpTileData& tmp, ...)`，注意是**非 const 引用**——指令要往里写进度。

**后端实现侧的消费**——以 CPU 模拟器为例：

- [include/pto/cpu/TMrgSort.hpp:L240-L252](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TMrgSort.hpp#L240-L252)：`TMRGSORT_IMPL` 把四个成员逐一拆传给底层 `TMrgsort` 模板；第 240-245 行先用 `kElemsPerStruct = STRUCT_BYTES / sizeof(T)` 把 validCol 换算成「8 字节结构」的个数——即对小 dtype，上报口径按 8 字节结构计（`STRUCT_BYTES = 8`），文档则按元素口径描述。

**语义文档**：

- [docs/isa/TMRGSORT.md:L86-L95](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TMRGSORT.md#L86-L95)：`exhausted = true` 时「任一 list 耗尽即暂停并经 `executedNumList` 上报每个 list 已处理元素数」的协议原文，并附同一结构体的文档版定义。

**另一组公共类型别名——随机数指令的种子**：

- [include/pto/common/type.hpp:L441-L444](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L441-L444)：`PTO_RANDOM_KEY_SIZE = 2`、`PTO_RANDOM_COUNTER_SIZE = 4` 两个常量，及 `TRandomKey = uint32_t[2]`、`TRandomCounter = uint32_t[4]` 两个别名。
- [include/pto/common/pto_instr.hpp:L676](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L676)：`TRANDOM(DstTile& dst, TRandomKey& key, TRandomCounter& counter, ...)`——key/counter 同样以非 const 引用传入、随调用演进。

#### 4.4.4 代码实践：给公共结构体做一次「布局体检」

**实践目标**：用编译期断言验证 `MrgSortExecutedNumList` 的内存布局，并把「公共结构体必须定义在 common 层」的结论落到可编译的证据上。

**操作步骤**：

1. 写下面的程序（示例代码）：

   ```cpp
   // 示例代码：struct_layout.cpp —— 验证公共结构体的布局与可见性
   #include <pto/pto-inst.hpp>

   static_assert(sizeof(pto::MrgSortExecutedNumList) == 8, "4 x uint16_t = 8 字节");
   static_assert(alignof(pto::MrgSortExecutedNumList) == 2, "uint16_t 的自然对齐");

   int main()
   {
       pto::MrgSortExecutedNumList executedNumList{};
       executedNumList.mrgSortList0 = 1;  // 与 ST 用例 tmrgsort_kernel.cpp 的用法一致
       return 0;
   }
   ```

2. 编译：`g++ -std=c++20 -D__CPU_SIM -Iinclude struct_layout.cpp -o struct_layout && ./struct_layout`。
3. 对照阅读 [tests/cpu/st/testcase/tmrgsort/tmrgsort_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tmrgsort/tmrgsort_kernel.cpp) 中 `MrgSortExecutedNumList executedNumList;` 后紧接 `TMRGSORT<...>(dstTile, executedNumList, ...)` 的真实用法。

**需要观察的现象**：两条 `static_assert` 通过、程序静默退出；只包含 `<pto/pto-inst.hpp>` 一个头就能拿到完整定义（不需要 include 任何后端头）。

**预期结果**：8 字节 / 2 字节对齐成立。进一步思考题：`uint16_t` 意味着单轮上报上限是 65535——结合 4.3 的 `REPEAT_MAX` 想一想，一个 tile 单批数据量本就受 UB 容量约束（远小于 65535 个 8 字节结构），所以 16 位计数不会成为瓶颈；这也解释了为什么协议设计者敢于用 `uint16_t` 而不是 `uint32_t`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MrgSortExecutedNumList` 不能定义在某个后端的 `TMrgSort.hpp` 里？

**答案**：公共声明 `pto_instr.hpp` 的 TMRGSORT 签名（[pto_instr.hpp:L869-L906](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L869-L906)）需要它是完整类型（参数按引用传递至少需要声明，而 cpu 实现还要访问成员则需要完整定义）；若定义在后端头里，公共声明就得反过来包含后端头，破坏「common 不依赖后端」的分层方向。本版本把它补进 type.hpp 正是修复这一悬空引用。

**练习 2**：TMRGSORT 为什么不干脆要求用户传入每个 list 的总长度，一次归并完？

**答案**：变长序列（如 MoE 场景里每个 expert 的 token 数事先未知）的总长度可能远超单个 tile 的容量；tile 编程模型一次只能装「一批」数据。「耗尽即暂停 + 上报进度」让指令在批间可续传：每轮只处理装得下的部分，剩余序列靠 `mrgSortListN` 记账。这正是 tile 级抽象表达不规则长度的典型手法（对照 [docs/isa/TMRGSORT.md:L86-L95](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TMRGSORT.md#L86-L95) 的 Variant B）。

**练习 3**：`TRandomKey`/`TRandomCounter` 为什么用「裸数组别名」而不是 struct？

**答案**：它们只是固定长度的字序列（key 2 字、counter 4 字，[type.hpp:L441-L444](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L441-L444)），没有按名访问单个成员的需求，数组别名最简单且与底层硬件寄存器组的装载形状一致；而 `MrgSortExecutedNumList` 的四个成员要**按 list 编号语义访问**（哪个 list 消费了多少），具名成员可读性更好。选择「struct 还是数组别名」应服从使用方式。

### 4.5 内核元信息：kernel_meta 与 .ascend.meta 段

#### 4.5.1 概念说明

SYNCALL（跨核栅栏，u3-l4 详讲）有一个特殊需求：**同一个算子可能同时跑在 AIC（Cube 核）和 AIV（Vector 核）上**，运行时加载内核 ELF 时，必须知道「这份 ELF 是纯 AIC、纯 AIV、还是混合主核」，以及 AIC:AIV 的任务配比。这些信息不能等内核跑起来再问，要在**链接期**就写进 ELF 的专用元数据段——`.ascend.meta.<kernelName>`。`kernel_meta.hpp` 就是往这个段里放数据的工具。

它采用 **TLV（Type-Length-Value）编码**：每条元数据 = 2 字节类型 + 2 字节长度 + 定长载荷，紧凑且运行时易于扫描。

#### 4.5.2 核心流程

```text
内核源文件中调用宏（如 PTO_SYNCALL_AIV_KERNEL_META(RunKernel)）
        │
        ▼
宏展开为一个 static const PtoMetaFunLevelMixCoreType 变量，
放置到 section ".ascend.meta.RunKernel"，并加 used 属性防止被优化掉
        │        （__COUNTER__ 保证同一文件多次调用宏变量名不冲突）
        ▼
链接期：变量进入内核 ELF 的 .ascend.meta 段
        │
        ▼
CCE 运行时加载 ELF：扫描 TLV → 读出核类型（AIC only / mix AIC main / mix AIV main）
        与任务配比（taskRation0 : taskRation1）→ 决定调度方式
```

#### 4.5.3 源码精读

**TLV 结构体**：

- [include/pto/common/kernel_meta.hpp:L17-L22](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/kernel_meta.hpp#L17-L22)：TLV 类型码（核类型 = 1，任务配比 = 3）与核类型取值（AIC only = 1，mix AIC main = 4，mix AIV main = 5）。
- [include/pto/common/kernel_meta.hpp:L24-L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/kernel_meta.hpp#L24-L43)：`PtoMetaBaseTlv`（4 字节头）、`PtoMetaKType`（+4 字节核类型）、`PtoMetaMixCoreType`（+2+2 字节配比）、组合体 `PtoMetaFunLevelMixCoreType`。整个结构体恰为 16 字节（两条 TLV 各 8 字节）。

**三个放置宏**：

- [include/pto/common/kernel_meta.hpp:L49-L65](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/kernel_meta.hpp#L49-L65)：`PTO_SYNCALL_AIV_KERNEL_META`（AIV 主）、`PTO_SYNCALL_MIX_AIC_KERNEL_META`（AIC 主，可指定 `aicRatio:aivRatio`）、`PTO_SYNCALL_AIC_KERNEL_META`（纯 AIC）。三者都用 `__attribute__((used, section(".ascend.meta." #kernelName)))` 定位，用 `PTO_DETAIL_CONCAT(... , __COUNTER__)` 生成唯一变量名。

**真实使用现场**（NPU 侧 ST 测试）：

- [tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp:L16](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a2a3/src/st/testcase/syncall/syncall_kernel.cpp#L16)：`PTO_SYNCALL_AIV_KERNEL_META(RunSyncAll_mix_aiv);`
- [tests/npu/a2a3/src/st/testcase/syncall/syncall_mix_1_1_kernel.cpp:L29](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a2a3/src/st/testcase/syncall/syncall_mix_1_1_kernel.cpp#L29)：1:1 混合核的用法。
- 使用说明文档：[docs/isa/tile/ops/sync-and-config/syncall.md:L88-L98](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/tile/ops/sync-and-config/syncall.md#L88-L98)。

#### 4.5.4 代码实践：画出元信息的字节布局（源码阅读型）

**实践目标**：不写代码，纯读源码画出 `PTO_SYNCALL_AIV_KERNEL_META(K)` 展开变量的 16 字节内存图，并理解每个字段的来源。

**操作步骤**：

1. 读 [kernel_meta.hpp:L24-L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/kernel_meta.hpp#L24-L43) 的四个结构体，标注每个成员的偏移与大小。
2. 对照宏 `PTO_SYNCALL_AIV_KERNEL_META`（L49-L53）的初始化列表，逐字段填值。
3. 用 `sizeof` 验证你的布局图（示例代码）：

   ```cpp
   // 示例代码：meta_size.cpp —— 仅复刻结构体定义验证布局，不依赖 NPU 环境
   #include <cstdint>
   #include <cstdio>
   struct PtoMetaBaseTlv { uint16_t type; uint16_t len; };
   struct PtoMetaKType { PtoMetaBaseTlv head; uint32_t ktype; };
   struct PtoMetaMixCoreType { PtoMetaBaseTlv head; uint16_t taskRation0; uint16_t taskRation1; };
   struct PtoMetaFunLevelMixCoreType { PtoMetaKType ktypeMeta; PtoMetaMixCoreType mixCoreType; };
   int main()
   {
       static_assert(sizeof(PtoMetaFunLevelMixCoreType) == 16, "两条 TLV 共 16 字节");
       printf("ktypeMeta=%zu, mixCoreType=%zu, total=%zu\n", sizeof(PtoMetaKType),
              sizeof(PtoMetaMixCoreType), sizeof(PtoMetaFunLevelMixCoreType));
       return 0;
   }
   ```

**需要观察的现象**：输出 `ktypeMeta=8, mixCoreType=8, total=16`，`static_assert` 通过。

**预期结果**：你的字节图应为——

| 偏移 | 大小 | 字段 | AIV 宏下的值 |
| --- | --- | --- | --- |
| 0 | 2 | `head.type` | `PTO_META_F_TYPE_KTYPE` = 1 |
| 2 | 2 | `head.len` | `sizeof(uint32_t)` = 4 |
| 4 | 4 | `ktype` | `PTO_META_K_TYPE_MIX_AIV_MAIN` = 5 |
| 8 | 2 | `head.type` | `PTO_META_F_TYPE_MIX_TASK_RATION` = 3 |
| 10 | 2 | `head.len` | 4 |
| 12 | 2 | `taskRation0` | 0 |
| 14 | 2 | `taskRation1` | 1 |

（AIV 主核时配比为 0:1，见宏初始化列表末尾的 `0, 1`。）

#### 4.5.5 小练习与答案

**练习 1**：为什么宏里要用 `__COUNTER__` 拼变量名？

**答案**：同一个 .cpp 里可能对多个内核各放一条元信息，宏展开成 `static const` 变量时若名字固定会重定义；`__COUNTER__` 每次展开自增，保证 `g_pto_syncall_aiv_meta_0`、`g_pto_syncall_aiv_meta_1`…互不冲突（[kernel_meta.hpp:L47-L53](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/kernel_meta.hpp#L47-L53)）。

**练习 2**：`used` 属性和独立 section 各自解决什么问题？

**答案**：`used` 防止编译器看到「没有任何代码引用这个变量」而把它删除——元信息的消费者是运行时而不是本编译单元；`section(".ascend.meta.<kernelName>")` 则让链接器把数据聚到命名的 ELF 段里，运行时按段名（含内核名）直接定位，不必扫描符号表。

**练习 3**：为什么这套机制只在 SYNCALL 场景需要？

**答案**：普通内核由 host 侧 launch 时已明确指定 AIV 或 AIC；而 SYNCALL 要求**一组 AIC/AIV 核互相同步**，运行时必须预先知道参与者的核型与配比才能正确组队（软栅栏还要按配比分配同步 workspace）。核型信息属于 ELF 级元数据，所以走链接期注入而非运行时传参。

## 5. 综合实践

把本讲五个模块串起来，完成一份《TADD 类型体检报告》：

1. **建表**（4.1）：完成「PTO 类型 → C++ 类型 → TADD 支持情况」速查表；再用 `include/README.md` 状态表补一列「该类型可用的其他典型指令类别」（元素级 / 规约 / Cube / 搬运 / 通信）。
2. **验证**（4.2）：执行 4.2.4 的三步实验——CPU 上 `int8_t` 编译通过（对照 [cpu/TAdd.hpp:L63-L75](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L63-L75) 无 dtype 检查）、复刻断言触发报错（对照 [a2a3/TAdd.hpp:L62-L66](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L62-L66)）、确认 A5 白名单包含 `int8_t`（[a5/TAdd.hpp:L59-L64](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAdd.hpp#L59-L64)）。
3. **推导**（4.3）：对表中每种 dtype 计算 `elementsPerRepeat` 与 `blockSizeElem`，并回答：哪个 dtype 的单 repeat 元素数恰好等于 `BLOCK_LEN`？再用 `BIT_TO_BYTE` 解释 [TRem.hpp:L211](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp#L211) 为什么要把 `RowStride`（位）除以 8 才得到掩码字节数。
4. **体检公共结构体**（4.4）：跑通 4.4.4 的布局断言；然后回答——如果 `MrgSortExecutedNumList` 被移到 `npu/a2a3/TMrgSort.hpp` 里定义，公共签名 `pto_instr.hpp` 与 CPU 模拟器路径分别会在哪一步编译失败？
5. **延伸**（4.5）：写 3-5 句话说明：如果一对 SYNCALL 混合核内核（AIC 主 1:1）忘记调用 `PTO_SYNCALL_MIX_AIC_KERNEL_META`，运行时会损失什么信息、可能出什么错（提示：运行时无法从 ELF 得知核型与配比；可对照 [docs/isa/tile/ops/sync-and-config/syncall.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/tile/ops/sync-and-config/syncall.md) 验证你的推测，不确定处标注「待确认」）。

产出物：一张速查表 + 一段实验记录（含编译器报错原文）+ 一组派生参数计算 + 一段结构体分析 + 一段元信息分析。

## 6. 本讲小结

- **类型别名**：`type.hpp` 末尾在 CPU/CostModel 分支下把 `half`/`aclFloat16`/`float16_t` 映射到 `_Float16`、`bfloat16_t` 按工具链能力三档取值；NPU 分支零定义、直接用 CCE 内建类型——这是「一份代码多后端」的第一块基石。
- **编译期宏**：`AICORE`/`PTO_INST`/`PTO_INTERNAL`/`OP_NAME` 让同一份函数声明在两种编译器下都合法；`PTO_STATIC_ASSERT` 用统一前缀 + 条件原文 + 文档线索格式化诊断。本版本还清理了 type.hpp 中多余的 `#include <type_traits>`——别依赖传递包含。
- **约束分布**：dtype 合法性检查在各 NPU 后端的 Check 函数（如 a2a3/a5 的 `TAddCheck`），且 **A2A3 与 A5 白名单不同**；CPU 模拟器不查 dtype——「CPU 跑通 ≠ 全后端合法」。
- **容量常量**：`constants.hpp` 定义「32 字节块 / 256 字节 repeat / 16 元素 / 512 字节 Cube 块 / 16 行分形」这组度量衡，指令实现用 \( 256/\text{sizeof}(T) \)、\( 32/\text{sizeof}(T) \) 现场推导参数；本版本新增 `BIT_TO_BYTE` 专司位↔字节换算（TRem 的掩码计算）；UB/L1 总容量在 `buffer_limits.hpp`。
- **公共结构体**：凡是指令签名要用的聚合类型必须定义在 common 层——`MrgSortExecutedNumList`（TMRGSORT 的逐 list 进度上报，本版本落位 type.hpp）与 `TRandomKey/TRandomCounter`（TRANDOM 的种子）是两个代表。
- **内核元信息**：`kernel_meta.hpp` 用 16 字节 TLV + `.ascend.meta.<kernelName>` 段，在链接期向运行时声明 SYNCALL 内核的核型与 AIC:AIV 配比。

## 7. 下一步学习建议

本讲解决的是「类型与常量从哪来」；下一讲 [u2-l2：GlobalTensor](u2-l2-globaltensor.md) 进入第一个真正的数据结构：`Shape`/`Stride` 如何用「静态维度 + 动态维度」描述 GM 上的五维张量（其 `TOTAL_DIM = 5` 的几何约束正是本讲 4.3 出现过的 `GlobalTensorDim`）。建议顺带阅读：

- [include/pto/common/memory.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/memory.hpp) 与 [docs/coding/GlobalTensor.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/coding/GlobalTensor.md)——预习下一讲；
- 想先了解 `TileType::Vec/Mat/Acc` 枚举如何映射到 UB/L1/L0C 各块缓冲，可跳读 [include/pto/common/buffer_limits.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/buffer_limits.hpp) 的分区注释（u2-l4 系统展开）；
- 对本讲 4.4 的 TMRGSORT 续传协议感兴趣的同学，可以提前翻看 [docs/isa/TMRGSORT.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TMRGSORT.md) 的 Variant B 与 [tests/cpu/st/testcase/tmrgsort](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tmrgsort/tmrgsort_kernel.cpp) 用例，体会变长序列如何映射到 tile 世界；
- 本讲 4.2 触发过的 `docs/coding/debug.md` 值得通读一遍，它就是所有 `[PTO][SA]`/`[PTO][CA]` 断言消息指引的排查手册。
