# OM2 格式转换子系统

## 1. 本讲目标

本讲是 OM2 运行时（单元 6）的进阶篇，承接上一讲 **u6-l6 OM2 模型格式与运行时执行**。在 u6-l6 里我们看到：OM2 把整张模型 codegen 成原生 `.so`，运行时几乎「直接执行编译产物」，开销极低。但模型里的 Variable（权重）常量在落盘时用的是一种排布，在设备上需要用另一种排布——这就需要在 Host 侧把张量**重排布（reformat）**后再搬运到设备。这套重排布能力，就由本讲的 **OM2 格式转换子系统（`runtime/om2/formats`）** 提供。

学完本讲，你应该能够：

1. 说清 `FormatTransfer` 抽象类定义的「转换契约」是什么，`TransArgs` / `TransResult` 两个结构分别承载什么。
2. 读懂一个具体格式对（如 `NCHW → NC1HWC0`）的转换器是如何实现的，包括排布变换的数学公式与多层数据搬运循环。
3. 说清 `REGISTER_FORMAT_TRANSFER` 宏 + 全局注册表 + `BuildFormatTransfer` 工厂，是如何让一个格式对在程序启动时被登记、在需要时按 `(src, dst)` 被选中的——这是一种典型的「自注册 + 工厂」开闭原则设计。
4. 把这套子系统与 u6-l6 的 `Om2RTVarManager::TransVarOnHost` 串起来，理解它「为谁服务、何时被调用」。

## 2. 前置知识

### 2.1 张量的「格式」= 内存放布方式

同一个四维张量，逻辑上是 `(N, C, H, W)`（批次、通道、高、宽），但它在内存里可以有不同的**存放顺序/排布**，这就是 GE 里的 **Format**（格式）。常见的逻辑格式有：

- `NCHW`：通道维在最前（紧随 N），CPU/GPU 上最常见。
- `NHWC`：通道维在最后，TensorFlow 风格。
- `HWCN`：权重常用。
- `ND`：任意维（N-dim），不限定排布。

这些只是「逻辑上等价、内存里不同」，转换它们本质是**按一套下标映射规则把每个元素搬到新位置**，数据本身不变。

### 2.2 为什么还需要「块格式」NC1HWC0 / FRACTAL_Z

昇腾 AI 核心的 Cube 矩阵运算单元一次处理一个固定大小的数据块（典型为 16×16，对 1 字节数据为 32），为了让硬件能一次性、对齐地把一块数据搬进 Cube，GE 引入了一组**块格式（block format）**，把通道维 `C` 按 `C0`（块大小，cube size）切分并补齐：

- `NC1HWC0`：把 `C` 拆成 `C1 = ⌈C / C0⌉` 份，每份 `C0` 个通道，并补 0 对齐。这样 `(N, C1, H, W, C0)` 五维布局里，最内层 `C0` 个元素正好是一个 Cube 块。
- `FRACTAL_Z` / `FRACTAL_ZZ` / `FRACTAL_NZ`：更复杂的分块排布，常用于权重，让 Cube 取数时地址连续。

> 直觉：逻辑格式是给人/框架看的，块格式是给硬件吃的。模型里常量权重往往以逻辑格式（如 NCHW）存盘，但设备要的是块格式，于是运行期必须做一次 reformat。

`C0` 不是固定值，而是按**数据类型**决定：2 字节及以上（fp16/fp32）`C0 = 16`，1 字节（int8）`C0 = 32`。本讲的 `GetCubeSizeByDataType` 就是干这个的。

### 2.3 primary / sub 格式：一个 Format 值的编码

GE 里一个 `Format` 枚举值可能同时编码「主格式」和「子格式」。工程上约定：

- **主格式（primary）** = `Format 值 & 0xff`（低 8 位），如 `FORMAT_NCHW`、`FORMAT_NC1HWC0`。
- **子格式（sub）** = `(Format 值 & 0xffff00) >> 8`（中间字节），用于在同一种排布下携带变体信息（如不同 C0）。

注册表是按 **(src 主格式, dst 主格式)** 这个二元组来登记和查找转换器的，所以 `formats.cc` / 各转换器里反复出现 `GetPrimaryFormat(...)` 的调用——它把带子格式的完整 Format 拆出主格式再去匹配注册表。子格式信息（如具体的 `c0`）则通过 `GetC0Value(...)` 单独取用。（这些拆分辅助函数由 `graph_metadef` 提供，本讲不展开其实现。）

### 2.4 与 u6-l6 的衔接

u6-l6 介绍了 `Om2RTVarManager` 管理变量的设备地址与数据搬运，其中提到变量数据搬运有三条路径：`init_data` 内嵌、`trans_road` 转换、`copy_info` 拷贝。**`trans_road` 这条路径就是本讲子系统的唯一运行时消费者**——当一条转换路上出现 `TRANSDATA` / `TRANSPOSED` 节点时，`Om2RTVarManager` 会调用本讲的 `ge::formats::TransDataFormat` 在 Host 上把数据重排布。理解这一点，你才会明白本讲这套「抽象 + 注册表 + 转换器」为什么是必要的。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `runtime/om2/formats/` 下（OM2 运行时的一个相对独立的子模块）：

| 文件 | 作用 |
| --- | --- |
| [runtime/om2/formats/formats.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/formats.h) | 对外统一入口声明：`TransDataFormat` / `TransTensorShape` / `IsTransFormatSupport`。 |
| [runtime/om2/formats/formats.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/formats.cc) | 入口实现：从注册表取出转换器并调用。 |
| [runtime/om2/formats/register_format_transfer.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.h) | 核心抽象：`TransArgs`/`TransResult` 结构、`FormatTransfer` 抽象类、注册宏 `REGISTER_FORMAT_TRANSFER`、工厂 `BuildFormatTransfer`。 |
| [runtime/om2/formats/register_format_transfer.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.cc) | 注册表实现：嵌套 map `(src → dst → builder)`，启动期自注册，运行期按主格式对查找。 |
| [runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.h) / [.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.cc) | 具体转换器实例：`NCHW → NC1HWC0`，含排布变换数学与多层数据搬运循环。 |
| [runtime/om2/formats/utils/formats_definitions.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/utils/formats_definitions.h) | 维度下标枚举（`kNchwN…`）与常量（`kCubeSize=16`）。 |
| [runtime/om2/formats/utils/formats_trans_utils.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/utils/formats_trans_utils.h) / [.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/utils/formats_trans_utils.cc) | 通用工具：`Ceil`、`GetItemNumByShape`、`GetCubeSizeByDataType`、shape 合法性检查。 |
| [runtime/om2/om2_rt_var_manager.cc](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc) | 消费者：`TransVarOnHost` 在变量搬运时调用 `TransDataFormat`（承接 u6-l6）。 |

整个子目录 `format_transfers/` 下还有十余个成对的转换器文件（`*_nchw.nc1hwc0` / `*_nc1hwc0_nchw`、`*_fractal_z`、`*_fractal_nz`、`*_transpose` 等），它们都用同一套抽象与注册机制，本讲以 `NCHW → NC1HWC0` 为代表讲透。

## 4. 核心概念与源码讲解

本讲拆为四个最小模块，对应规范要求的三大主题（抽象、互转实现、注册与选择），再补一个「统一入口 + 运行时消费者」把它们串到 u6-l6。

### 4.1 FormatTransfer 抽象：一切转换器的契约

#### 4.1.1 概念说明

「格式转换」要做的事情，抽象地说只有两件：

1. **TransShape（算形）**：给定源格式 + 源 shape + 数据类型 + 目标格式，算出目标 shape。例如 `NCHW` 的 `(1,3,224,224)` 在 fp16 下转 `NC1HWC0`（C0=16）得到 `(1,1,224,224,16)`。
2. **TransFormat（搬数据）**：给定源数据指针 + 源/目标 shape + 各类格式参数，按排布规则把每个元素搬到目标缓冲区，产出 `TransResult`。

GE 用一个纯虚抽象类 `FormatTransfer` 把这两件事定为「契约」：每一种具体格式对（如 `NCHW↔NC1HWC0`、`NCHW↔FRACTAL_Z`）都实现一个子类，提供自己的 `TransFormat` 与 `TransShape`。调用方只面向基类接口，不关心具体是哪种转换。

支撑这个契约的还有两个数据结构：

- **`TransArgs`**：一次转换的全部入参——源数据指针、源/目标的（完整格式、主格式、子格式、c0）、源 shape、目标 shape、数据类型。它把「主/子/c0」显式拆开，是因为排布计算既要靠主格式找转换器，又要靠 c0 / 子格式决定具体分块。
- **`TransResult`**：转换产物——一块 `shared_ptr<uint8_t>` 数据及其字节长度。

#### 4.1.2 核心流程

一次格式转换在抽象层面的流程：

```text
调用方组装 TransArgs（src/dst 格式、shape、数据指针、dtype）
        │
        ▼
BuildFormatTransfer(args)        ← 按 (src 主格式, dst 主格式) 从注册表取出转换器
        │  （取不到 → 该格式对不支持）
        ▼
transfer->TransFormat(args, result)   ← 多态分派到具体子类的搬数据实现
        │
        ▼
TransResult（重排布后的数据 + 长度）
```

`TransShape` 是「只算形状不搬数据」的旁路，常用于事先校验或预分配缓冲。

#### 4.1.3 源码精读

`TransArgs` 与 `TransResult` 定义了转换的「语言」，注意它们把主/子/c0 都显式列出：

[register_format_transfer.h:25-48](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.h#L25-L48) — `TransArgs` 同时携带 `src_format`（完整格式）与 `src_primary_format`/`src_sub_format`/`src_c0_format`（拆解后的主/子/c0），`TransResult` 持有数据指针与字节长度。

抽象契约 `FormatTransfer` 极其精简，只有两个纯虚方法，且禁用拷贝：

[register_format_transfer.h:57-68](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.h#L57-L68) — 纯虚 `TransFormat` 与 `TransShape` 定义了所有具体转换器必须实现的两个动作；`= delete` 拷贝构造/赋值避免转换器被意外复制。

「构造一个转换器」的能力被抽象成一个工厂函数类型 `FormatTransferBuilder`（无参、返回 `shared_ptr<FormatTransfer>`），这是注册表里登记的东西：

[register_format_transfer.h:70-76](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.h#L70-L76) — `FormatTransferBuilder` 是「能造出一个 `FormatTransfer` 的工厂函数」；`FormatTransferRegister` 是配合自注册宏用的「登记员」对象。

#### 4.1.4 代码实践

**实践目标**：在不读具体转换器的前提下，仅凭抽象头文件画出「一次转换的数据流」。

**操作步骤**：

1. 打开 [register_format_transfer.h](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.h)。
2. 找到 `TransArgs`（25 行起）与 `TransResult`（44 行起），列出它们各自有哪些字段。
3. 找到 `FormatTransfer` 类（57 行起），确认它只有 `TransFormat`、`TransShape` 两个虚方法。
4. 找到 `BuildFormatTransfer`（82 行）与 `FormatTransferExists`（84 行），猜一下它们的职责。

**需要观察的现象**：`TransArgs` 把 `primary`/`sub`/`c0` 与完整 `format` **同时**保留；这说明查找转换器用的是 primary，而真正算排布要靠 c0。

**预期结果**：你能用一句话回答——「`FormatTransfer` 是契约，`TransArgs` 是入参信封，`TransResult` 是回信，`BuildFormatTransfer` 是按格式对取实现的中转站。」

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FormatTransfer` 把 `TransShape` 和 `TransFormat` 拆成两个方法，而不是合成一个？

> **参考答案**：因为「算目标 shape」和「搬数据」是两个独立的诉求。有时只想预判目标 shape 是否合法、或预先分配缓冲（用 `TransShape`），并不需要真把数据搬一遍；有时 shape 已经确定、只管搬数据（用 `TransFormat`）。拆开后调用方按需取用，也便于把「算形」复用到 shape 校验等场景。

**练习 2**：`TransArgs` 里已经有 `src_format`，为什么还要单独列 `src_primary_format`？

> **参考答案**：注册表按 **(主格式, 主格式)** 二元组登记和查找，而完整的 `src_format` 还携带子格式信息。把 primary 单独拆出来，是为了让查找逻辑直接拿到「主格式键」；子格式/c0 则留给具体转换器内部决定分块参数时使用。

---

### 4.2 转换器的注册与按格式对选择

#### 4.2.1 概念说明

抽象类只定义了「长什么样」，还要解决「系统怎么知道 `NCHW → NC1HWC0` 这个具体转换器存在、怎么在运行时找到它」。GE 用的是一套经典的 **自注册（self-registration）+ 工厂（factory）** 模式：

- 一个**全局注册表**，按 `(src 主格式 → dst 主格式 → builder)` 三层嵌套存放「能造出转换器的工厂函数」。
- 每个具体转换器 `.cc` 文件末尾用宏 `REGISTER_FORMAT_TRANSFER(类名, src格式, dst格式)` 声明一个**静态全局对象**，该对象在程序启动时（`main` 之前）自动把自己登记进注册表。
- 运行时 `BuildFormatTransfer(args)` 拿 `(args.src_primary_format, args.dst_primary_format)` 去注册表里查，查到就调对应 builder 造一个实例返回，查不到返回 `nullptr`（表示该方向不支持）。

这种设计的好处是**开闭原则**：新增一个格式对，只要新写一个 `.cc`、加一行宏，**完全不用改动注册表或入口代码**。

#### 4.2.2 核心流程

```text
【启动期·自注册】
  每个格式对 .cc 末尾的 REGISTER_FORMAT_TRANSFER 宏
        │  展开为：一个匿名工厂函数 + 一个静态 FormatTransferRegister 全局对象
        ▼
  FormatTransferRegister 构造 → GetFormatTransferRegistry().RegisterBuilder(src,dst,builder)
        │
        ▼
  全局表 src_dst_builder[src][dst] = builder   （嵌套 std::map）

【运行期·查找】
  BuildFormatTransfer(args)
        │  用 (src_primary, dst_primary) 在嵌套 map 里 find
        ▼
  找到 → 调 builder() 造一个 FormatTransfer 实例返回
  没找到 → 返回 nullptr
```

注意查找用的是 **primary 主格式**，所以 `NC1HWC0` 带不带子格式都能命中同一个转换器。

#### 4.2.3 源码精读

注册表本身是一个匿名命名空间里的结构体，核心数据结构是嵌套 `std::map`：

[register_format_transfer.cc:18-48](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.cc#L18-L48) — `FormatTransferRegistry` 用 `std::map<Format, std::map<Format, FormatTransferBuilder>>` 作为 `(src → dst → builder)` 三级索引；`RegisterBuilder` 写入，`GenerateFormatTransfer` 两级 `find` 取出 builder 并调用，`IsFormatTransferExists` 只判存在性。

注册表是「函数内静态变量」单例，首次访问时构造，保证全局唯一且线程安全初始化：

[register_format_transfer.cc:50-53](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.cc#L50-L53) — `GetFormatTransferRegistry()` 返回一个 `static` 局部变量，是典型的 Meyers 单例。

「登记员」对象在构造时调用 `RegisterBuilder`，这是自注册的关键一环：

[register_format_transfer.cc:56-60](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.cc#L56-L60) — `FormatTransferRegister` 构造函数把 builder 写进全局表。

运行期查找入口——注意它只取 primary：

[register_format_transfer.cc:62-68](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.cc#L62-L68) — `BuildFormatTransfer` / `FormatTransferExists` 都用 `args.src_primary_format` 与 `args.dst_primary_format` 作键。

把这一切串起来的自注册宏，展开为一个工厂函数加一个静态登记对象：

[register_format_transfer.h:88-95](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.h#L88-L95) — 宏 `REGISTER_FORMAT_TRANSFER(类, fmt1, fmt2)` 在匿名命名空间里生成 `Transfer_<fmt1>_<fmt2>()` 工厂函数和一个静态 `FormatTransferRegister` 全局对象，对象构造时自动登记。

具体到 `NCHW → NC1HWC0`，它在文件末尾用一行宏完成登记：

[format_transfer_nchw_nc1hwc0.cc:261-261](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.cc#L261-L261) — `REGISTER_FORMAT_TRANSFER(FormatTransferNchwNc1hwc0, FORMAT_NCHW, FORMAT_NC1HWC0)`，等价于「在 `src=FORMAT_NCHW, dst=FORMAT_NC1HWC0` 这个格子登记一个能造 `FormatTransferNchwNc1hwc0` 的工厂」。

本子系统目前已登记的格式对覆盖了常见的逻辑↔块格式互转，下表列出有代表性的几组（同一转换器类可登记多个格式对）：

| 方向（src → dst） | 转换器类 | 文件 |
| --- | --- | --- |
| `NCHW → NC1HWC0` | `FormatTransferNchwNc1hwc0` | format_transfer_nchw_nc1hwc0.cc |
| `NC1HWC0 → NCHW` | `FormatTransferNc1hwc0Nchw` | format_transfer_nc1hwc0_nchw.cc |
| `NHWC → NC1HWC0` / `NC1HWC0 → NHWC` | `FormatTransferNhwcNc1hwc0` / `FormatTransferNc1hwc0Nhwc` | format_transfer_nhwc_nc1hwc0.cc / format_transfer_nc1hwc0_nhwc.cc |
| `NCHW/HWCN/NHWC → FRACTAL_Z` | `FormatTransferFractalZ` | format_transfer_fractal_z.cc |
| `FRACTAL_Z → NCHW/NHWC/HWCN` | `FormatTransferFracZNchw/Nhwc/Hwcn` | format_transfer_fracz_*.cc |
| `ND/NCHW/NHWC ↔ FRACTAL_NZ` | `FormatTransferFractalNz` / `...ND` | format_transfer_fractal_nz.cc |
| `ND/NCHW/NHWC ↔ FRACTAL_ZZ` | `FormatTransferFractalZz` / `...ND` | format_transfer_fractal_zz.cc |
| `NCHW/HWCN → FRACTAL_Z_C04` | `FormatTransfer4DToFZC04` | format_transfer_fz_c04.cc |
| `NCHW↔NHWC/HWCN/CHWN`（纯转置） | `FormatTransferTranspose` | format_transfer_transpose.cc |
| `HWCN ↔ C1HWNCoC0`、`DHWCN → FRACTAL_Z_3D` 等 | 各专用类 | format_transfer_c1hwncoc0_hwcn.cc 等 |

注意 `FormatTransferTranspose` 一个类登记了 12 个 NCHW/NHWC/HWCN/CHWN 之间的互转——因为它们之间都是纯维度转置，共用同一份实现。

#### 4.2.4 代码实践

**实践目标**：验证「新增格式对无需改注册表」的开闭原则，并追踪一次 `(NCHW, NC1HWC0)` 的查找路径。

**操作步骤**：

1. 打开 [register_format_transfer.h:88-95](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.h#L88-L95)，手把手把宏 `REGISTER_FORMAT_TRANSFER(FormatTransferNchwNc1hwc0, FORMAT_NCHW, FORMAT_NC1HWC0)` 展开成等价的「工厂函数 + 静态登记对象」C++ 代码。
2. 打开 [register_format_transfer.cc:18-44](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.cc#L18-L44)，沿着 `GenerateFormatTransfer` 的两级 `find`：先 `src_dst_builder.find(FORMAT_NCHW)`，再在结果里 `.find(FORMAT_NC1HWC0)`，确认能命中。
3. 在仓库里执行只读检索，确认没有任何「中心化 if-else 分支」根据格式名选择转换器——选择完全靠 map 查找。

```bash
# 仅检索、不修改：统计本子系统登记了多少个格式对
grep -rn "REGISTER_FORMAT_TRANSFER(" runtime/om2/formats/format_transfers/ | wc -l
```

**需要观察的现象**：宏展开后，每个格式对产生一个**静态全局对象**；这些对象的构造发生在 `main` 之前（动态库加载时），所以运行期注册表已经是满的。

**预期结果**：你能在纸上画出「`FORMAT_NCHW → FORMAT_NC1HWC0 → builder → FormatTransferNchwNc1hwc0 实例`」这条查找链，并解释「新增一个 `XYZ → FRACTAL_Z` 只需新写一个 `.cc` + 一行宏」。

#### 4.2.5 小练习与答案

**练习 1**：注册表为什么用嵌套 `map<Format, map<Format, builder>>` 而不是 `map<pair<Format,Format>, builder>`？

> **参考答案**：两者都能工作。嵌套 map 的好处是 `IsFormatTransferExists` 可以先按 src 一级查找、快速排除「该 src 格式没有任何已知转换」的情况，且语义上更贴近「以 src 为主键、dst 为次键」的索引视图。这只是实现取舍，不影响外部接口。

**练习 2**：如果运行期请求一个未登记的格式对（如 `FORMAT_ND → FORMAT_NCHW` 恰好没注册），会发生什么？

> **参考答案**：`BuildFormatTransfer` 在两级 `find` 中任一未命中即返回 `nullptr`；上层 `TransDataFormat` 见 `nullptr` 会记录 `ACL_ERROR_GE_FORMAT_INVALID` 错误并返回失败（见 4.4 节入口实现）。不会崩溃，只是报「不支持该格式对」。

---

### 4.3 格式互转的实现：以 NCHW → NC1HWC0 为例

#### 4.3.1 概念说明

抽象与注册解决了「找谁来做」，本模块解决「具体怎么做」。`NCHW → NC1HWC0` 是最有代表性的一例，因为它同时涉及 **shape 重算** 和 **带补零的数据搬运**。

核心是两条数学映射：

**目标 shape（算形）**。设源 shape 为 \((N, C, H, W)\)，块大小为 \(C_0\)（由数据类型决定，fp16/fp32 为 16）。则：

\[
C_1 = \left\lceil \frac{C}{C_0} \right\rceil, \qquad \text{dst\_shape} = (N,\ C_1,\ H,\ W,\ C_0)
\]

当 \(C\) 不能被 \(C_0\) 整除时，最末一个 \(C_0\) 块里会有补零位，因此目标元素总数 \(N \cdot C_1 \cdot H \cdot W \cdot C_0 \geq N \cdot C \cdot H \cdot W\)。

**下标映射（搬数据）**。逻辑元素 \((n, c, h, w)\) 在源 NCHW 行主序缓冲里的线性下标为：

\[
\text{srcIdx} = n \cdot C H W + c \cdot H W + h \cdot W + w
\]

在目标 NC1HWC0 缓冲里，同一个逻辑元素位于 \((n, c_1, h, w)\)，通道块内偏移 \(c_{off} = c \bmod C_0\)，其中 \(c = c_1 \cdot C_0 + c_{off}\)。目标线性下标为：

\[
\text{dstIdx} = n \cdot (C_1 H W C_0) + c_1 \cdot (H W C_0) + h \cdot (W C_0) + w \cdot C_0 + c_{off}
\]

代码用预计算几个步长（`hwc0 = hw*c0`、`c1hwc0 = c1*hwc0`）来组织这个映射，并按目标维度从外到内做六层循环逐元素搬运；当 \(c = c_1 C_0 + c_{off} \geq C\) 时（补零位）用 `memset` 填 0。

#### 4.3.2 核心流程

```text
TransFormat(args, result)
  │
  ├─ CheckArgsForNchwToNc1hwc0(args)        ← 校验主格式、算期望 5D shape、比对 dst_shape
  │      └─ TransShapeNchwToNc1hwc0(...)    ← 用 C0 算 (N,C1,H,W,C0)
  │
  ├─ size      = GetSizeByDataType(dtype)   ← 每个元素的字节数
  ├─ total_size= 目标元素数 × size           ← 目标缓冲总字节数
  │
  └─ GetDstDataAfterTransOfNchw2Nc1hwc0(...)
         ├─ new 一块 total_size 的目标缓冲
         ├─ 预计算 c1, hw, hwc0, c1hwc0 步长
         ├─ for n, c1, h, w, c0_off（6 层循环，按目标维度外→内）
         │     ├─ 算 dst_offset
         │     ├─ cIdx = c0_off + c1*C0
         │     ├─ 若 cIdx < C：从 src 对应 srcIdx 拷一个元素（memcpy_s）
         │     └─ 否则       ：该位补零（memset_s）
         └─ result.data / result.length 回填
```

`TransShape` 是旁路，直接复用同一套 `TransShapeNchwToNc1hwc0`，只算形状不分配缓冲。

#### 4.3.3 源码精读

转换器类声明非常薄——只声明两个虚方法，实现都在 `.cc`：

[format_transfer_nchw_nc1hwc0.h:19-24](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.h#L19-L24) — `FormatTransferNchwNc1hwc0` 继承 `FormatTransfer`，override `TransFormat` 与 `TransShape`。

算形逻辑直接对应公式 \(C_1=\lceil C/C_0\rceil\)，`Ceil` 与维度下标枚举都是本子系统的工具：

[format_transfer_nchw_nc1hwc0.cc:26-55](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.cc#L26-L55) — `TransShapeNchwToNc1hwc0` 先按数据类型取 `c0`（`GetCubeSizeByDataType`），校验源 shape 是 4 维，再用 `Ceil(C, c0)` 得 `C1`，按 `(N, C1, H, W, C0)` 回填 dst_shape。

其中 `c0` 来自数据类型，2 字节及以上为 16、1 字节为 32：

[formats_trans_utils.cc:21-34](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/utils/formats_trans_utils.cc#L21-L34) — `GetCubeSizeByDataType` 按每元素字节数返回 cube 块大小：1 字节返 `kCubeSize*2`（32），其余返 `kCubeSize`（16）。

`Ceil` 这个向上取整工具定义在 utils 头里，是本子系统的常用件：

[formats_trans_utils.h:62-67](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/utils/formats_trans_utils.h#L62-L67) — 模板 `Ceil(n1, n2)` 实现 `(n1-1)/n2 + 1` 的向上取整。

维度下标枚举避免到处写魔法数字 0/1/2/3：

[formats_definitions.h:23-31](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/utils/formats_definitions.h#L23-L31) — `NchwDimIndex { kNchwN, kNchwC, kNchwH, kNchwW, kNchwDimsNum=4 }` 与 `Nc1hwc0DimIndex { … kNc1hwc0DimsNum=5 }`，分别给源 4 维、目标 5 维下标命名。

搬运主循环的步长预计算，正是前面公式里的 \(C_1 H W C_0\)、\(H W C_0\) 等：

[format_transfer_nchw_nc1hwc0.cc:123-126](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.cc#L123-L126) — 预计算 `c1`、`hw=h*w`、`hwc0=hw*c0`、`c1hwc0=c1*hwc0` 四个步长，分别对应目标缓冲沿 n、c1、h、w 各维的跨度。

六层嵌套循环逐元素搬运，并在补零位用 `memset_s` 填 0：

[format_transfer_nchw_nc1hwc0.cc:128-193](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.cc#L128-L193) — 外层按 `n, c1, h, w, c0_off` 累加得到 `dst_offset`；算出逻辑通道 `cIdx = c0_off + c1_idx*c0`，若 `cIdx < c` 则按 `srcIdx` 从源拷一个元素，否则 `memset_s` 补零。`memcpy_s` / `memset_s` 与 `protected_size` 都是为了安全拷贝（防止越界）。

源下标计算 `srcIdx` 对应公式里的 \(n\cdot CHW + c\cdot HW + h\cdot W + w\)：

[format_transfer_nchw_nc1hwc0.cc:144-149](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.cc#L144-L149) — `cIdx` 还原逻辑通道，`srcIdx = n_idx*(c*hw) + cIdx*hw + h_idx*w + w_idx`，即 NCHW 行主序线性下标。

`TransFormat` 入口把校验、算总量、搬运串起来：

[format_transfer_nchw_nc1hwc0.cc:201-246](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.cc#L201-L246) — 先 `CheckArgsForNchwToNc1hwc0` 校验，再算 `total_size = 目标元素数 × 每元素字节数`，最后调 `GetDstDataAfterTransOfNchw2Nc1hwc0` 完成搬运。

#### 4.3.4 代码实践

**实践目标**：把公式与源码逐行对上，亲手验证一个极小例子的下标映射。

**操作步骤**：

1. 取一个极小例子：源 NCHW shape `(1, 3, 2, 2)`，fp16（\(C_0=16\)）。
2. 用公式算目标 shape：\(C_1=\lceil 3/16\rceil=1\)，故 dst = `(1,1,2,2,16)`，目标元素数 \(1\cdot1\cdot2\cdot2\cdot16=64\)，源元素数 \(1\cdot3\cdot2\cdot2=12\)，补零 \(64-12=52\) 位。
3. 对照 [format_transfer_nchw_nc1hwc0.cc:123-149](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.cc#L123-L149)，取逻辑元素 \((n=0, c=2, h=0, w=1)\)（源里第 \(2\cdot4+0\cdot2+1=9\) 个元素），手算它在目标里的位置：\(c_1=0,\ c_{off}=2\)，dstIdx \(= 0 + 0 + 0 + 1\cdot16 + 2 = 18\)。
4. 确认补零：目标里 `c0_off ∈ [3,16)` 的位置（每个 `c1` 块的后 13 个通道偏移）都应被 `memset_s` 填 0。

**需要观察的现象**：目标缓冲比源缓冲大很多（因 C0 补齐）；同一个逻辑通道的 \(C_0\) 个元素在目标里是**地址连续**的（最内维），这正是块格式对硬件友好的原因。

**预期结果**：你能在纸上列出「源第 9 个元素 → 目标第 18 个字节位（按元素计）」的正确映射，并解释为何补零不可避免。

> 说明：本实践为「源码阅读 + 手算验证」型，不依赖昇腾设备；若要在本机跑真实转换，需要先编译出 `om2_executor` 等动态库并具备 CANN 运行环境，相关构建方式见 u1-l3。具体运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把数据类型从 fp16 换成 int8，目标 shape 会怎样变化？

> **参考答案**：int8 每元素 1 字节，`GetCubeSizeByDataType` 返回 \(C_0=32\)。对 `(1,3,2,2)`，\(C_1=\lceil 3/32\rceil=1\)，dst = `(1,1,2,2,32)`，目标元素数 \(=128\)，补零更多。可见 \(C_0\) 越大、补零比例可能越高（当 C 远小于 C0 时）。

**练习 2**：六层循环为什么按 `n → c1 → h → w → c0_off` 的顺序嵌套，而不是别的顺序？

> **参考答案**：因为目标缓冲是按 `(N, C1, H, W, C0)` 行主序连续存放的，外层到内层的循环顺序正好让目标地址 `dst_offset` 单调连续增长、最内层 `c0_off` 步长为 1，写目标缓冲的访存模式最友好（顺序写）。源侧读取则按 `srcIdx` 跳跃，属于「读散、写顺」的典型 reformat 模式。

---

### 4.4 统一入口 TransDataFormat 与运行时消费者（衔接 u6-l6）

#### 4.4.1 概念说明

前面三个模块讲清了「抽象—注册—实现」。本模块把它们接到真实调用方，回答「这套子系统为谁服务」。它只有两个要点：

1. **统一入口** `TransDataFormat(args, result)`：一个薄薄的门面，先 `BuildFormatTransfer` 取转换器，做基本入参校验（数据非空、shape 合法），再委托给具体 `TransFormat`。所有格式对共用这一个入口，调用方无需知道具体转换器类型。
2. **运行时消费者** `Om2RTVarManager::TransVarOnHost`：u6-l6 讲过变量搬运的三条路径，其中 `trans_road` 路径上若出现 `TRANSDATA` / `TRANSPOSED` 节点，就调用本入口在 Host 上把数据重排布——这是本子系统在 OM2 里的唯一调用点，也是它存在的直接理由。

#### 4.4.2 核心流程

```text
Om2RTVarManager::TransVarOnHost(trans_road, data)          【承接 u6-l6】
  │  遍历转换路上的每个节点
  │  若 node_type ∈ {TRANSDATA, TRANSPOSED}：
  │     从 node.input/output 取 src/dst 格式、shape、dtype
  │     拆出 primary / sub / c0，组装 TransArgs
  ▼
ge::formats::TransDataFormat(args, tmp_result)             【本子系统统一入口】
  │  BuildFormatTransfer(args) → 取转换器（找不到报 FORMAT_INVALID）
  │  校验 data 非空 / shape 非空
  ▼
transfer->TransFormat(args, result)                         【多态到 4.3 的具体实现】
  │
  ▼
重排布后的数据 → 作为下一节点的输入，链式走完整条 trans_road → 最终搬运到设备
```

`TransTensorShape` 与 `IsTransFormatSupport` 是同源的旁路接口：前者只算形状（用于预校验/预分配），后者用 `FormatTransferExists` 判某个格式对是否被支持。

#### 4.4.3 源码精读

统一入口三个函数都极其薄，核心是「取转换器 + 委托」：

[formats.cc:30-54](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/formats.cc#L30-L54) — `TransDataFormat` 调 `BuildFormatTransfer` 取转换器，`nullptr` 即报 `ACL_ERROR_GE_FORMAT_INVALID`；再校验数据指针与 shape 元素数，最后委托 `transfer->TransFormat(args, result)`。

`TransTensorShape` 同样先取转换器（注意它用 `GetPrimaryFormat` 把传入格式拆成主格式再查表），再委托 `TransShape`：

[formats.cc:56-73](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/formats.cc#L56-L73) — `TransTensorShape` 组装一个仅含格式信息的 `TransArgs`，用 `GetPrimaryFormat` 填 `src/dst_primary_format`，取转换器后调 `TransShape`。

支持性判断直接转发到注册表的 `FormatTransferExists`：

[formats.cc:75-77](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/formats.cc#L75-L77) — `IsTransFormatSupport` 一行实现，转发到 `FormatTransferExists(args)`。

消费者侧——u6-l6 的 `Om2RTVarManager` 在变量搬运的转换路上调用本入口：

[om2_rt_var_manager.cc:275-318](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L275-L318) — `TransVarOnHost` 遍历 `trans_road`，对 `TRANSDATA` / `TRANSPOSED` 节点从 `node.input/output` 取格式与 shape，用 `GetPrimaryFormat` / `GetSubFormat` / `GetC0Value` 拆解后组装 `TransArgs`，调用 `ge::formats::TransDataFormat(...)`；结果作为链路上下一个节点的输入，逐级重排布直到得到设备所需格式。这条链正是 u6-l6 里「trans_road 转换路径」的具体实现。

> 子系统边界：从构建看，[runtime/om2/CMakeLists.txt:11-28](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/CMakeLists.txt#L11-L28) 把 `runtime/om2/*.cc` 编进 `om2_executor` 共享库，`runtime/om2/formats/*.cc` 随之一起编入（`GLOB_RECURSE`），所以这套格式转换能力随 OM2 执行器一起分发，是 OM2 运行时的内生组件。

#### 4.4.4 代码实践

**实践目标**：把本子系统与 u6-l6 的变量搬运链路对上，理解「转换器是被谁、在何时调用的」。

**操作步骤**：

1. 打开 [om2_rt_var_manager.cc:275-318](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L275-L318)，找到 `node.node_type == "TRANSDATA"` 分支里对 `ge::formats::TransDataFormat` 的调用。
2. 回看 [formats.cc:30-54](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/formats.cc#L30-L54)，确认 `TransDataFormat` 内部就是「取转换器 + 委托」。
3. 追踪 `last_result` / `tmp_result` 在循环里的传递：第一次用原始 `data`，之后每次用上一步的 `last_result` 作输入——这是一条**链式**重排布流水线。

**需要观察的现象**：一条 `trans_road` 可能有多个节点，每跑过一个 `TRANSDATA` 节点，数据就被重排布一次，最终形态由最后一个节点决定。

**预期结果**：你能用一句话讲清——「OM2 把变量的格式转换需求编码成一条 `trans_road`，`Om2RTVarManager` 逐节点调用本子系统的 `TransDataFormat` 在 Host 上完成重排布，再搬运到设备。」由此也回答了 u6-l6 留下的「trans_road 转换路径由谁实现」。

> 说明：此实践为源码阅读型，运行 `Om2RTVarManager` 需要完整的 OM2 模型归档与昇腾运行环境（见 u6-l6），具体行为**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`TransDataFormat` 与 `TransTensorShape` 都先调用 `BuildFormatTransfer`，二者区别在哪？

> **参考答案**：`TransDataFormat` 既校验又真正搬数据，需要 `args.data` 指向真实缓冲，产出 `TransResult`；`TransTensorShape` 不需要数据指针，只算目标 shape（用于预判/预分配），不分配大数据缓冲。两者共用「按格式对取转换器」的前置逻辑。

**练习 2**：为什么 `trans_road` 要设计成「多个节点链式转换」，而不是一步到位？

> **参考答案**：因为一个变量从存盘格式到设备格式，可能需要经过多次重排布（例如先 NCHW→NC1HWC0，再做一次转置或类型转换）。把每一步抽象成一个节点、用同一条路串起来，既复用了同一套 `TransDataFormat` 接口，又能灵活表达不同的转换组合，而不必为每种「复合转换」写专门的代码。

---

## 5. 综合实践

**任务**：以「新增一个假想的格式对」为线索，把本讲四个模块串起来设计（只做纸面设计，不改源码）。

假设你希望让 OM2 支持 `NCHW → NHWC`（实际上 `FormatTransferTranspose` 已覆盖，这里仅作练习载体），请完成：

1. **抽象落地**：参照 [format_transfer_nchw_nc1hwc0.h:19-24](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/format_transfers/format_transfer_nchw_nc1hwc0.h#L19-L24)，写出你的转换器类声明（继承 `FormatTransfer`，override 两个方法）。
2. **算形与映射**：写出 `NCHW (N,C,H,W)` → `NHWC (N,H,W,C)` 的 shape 映射，以及逻辑元素 \((n,c,h,w)\) 的 srcIdx / dstIdx 公式（提示：NHWC 是把通道维挪到最后，dstIdx \(= n\cdot HWC + h\cdot WC + w\cdot C + c\)）。
3. **注册**：参照 [register_format_transfer.h:88-95](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.h#L88-L95)，写出文件末尾应加的 `REGISTER_FORMAT_TRANSFER(...)` 一行，并说明它会在何时、把什么登记到哪里。
4. **查找验证**：沿着 [register_format_transfer.cc:18-44](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/formats/register_format_transfer.cc#L18-L44) 的两级 `find`，说明运行期 `(FORMAT_NCHW, FORMAT_NHWC)` 如何命中你的转换器。
5. **消费者衔接**：说明这个新转换器一旦登记，[om2_rt_var_manager.cc:275-318](https://github.com/gitcode.com/cann/ge/blob/aed3d571e68c9cd40f9c377ad3176e7c249791f8/runtime/om2/om2_rt_var_manager.cc#L275-L318) 里的 `TransVarOnHost` 无需任何改动就能在出现 `NCHW→NHWC` 需求时自动用上它——这正是开闭原则的体现。

**预期产出**：一段纸面设计，包含类声明草图、两个下标公式、一行注册宏、一条查找链，以及一句「为什么消费者无需改动」的解释。本任务无需设备，纯源码阅读与设计推演。

## 6. 本讲小结

- **抽象**：`FormatTransfer` 用 `TransFormat`（搬数据）+ `TransShape`（算形状）两个纯虚方法定义了所有格式转换器的统一契约；`TransArgs` 携带完整格式与拆解后的 primary/sub/c0，`TransResult` 回传数据与长度。
- **注册与选择**：全局嵌套 map `(src 主格式 → dst 主格式 → builder)` + `REGISTER_FORMAT_TRANSFER` 宏自注册 + `BuildFormatTransfer` 工厂查找，构成「开闭原则」式的扩展机制——新增格式对只写一个 `.cc` 加一行宏，不改中心代码。
- **查找键**：注册与查找都按 **primary 主格式** 进行，子格式/c0 仅在转换器内部决定具体分块参数。
- **互转实现**：`NCHW → NC1HWC0` 用 \(C_1=\lceil C/C_0\rceil\) 算 5D 目标 shape，用六层循环按目标维度顺序搬运，\(C\) 不整除 \(C_0\) 处补零；\(C_0\) 由数据类型决定（fp16/fp32 为 16，int8 为 32）。
- **入口**：`TransDataFormat` 是薄门面，`TransTensorShape` / `IsTransFormatSupport` 是同源旁路。
- **消费者**：u6-l6 的 `Om2RTVarManager::TransVarOnHost` 在变量搬运的 `trans_road` 上对 `TRANSDATA`/`TRANSPOSED` 节点调用本入口，链式完成 Host 侧重排布——这是本子系统存在的直接理由。

## 7. 下一步学习建议

- **横向对比 v1/v2 的格式转换**：本讲聚焦 OM2。GE 在 `graph_metadef/third_party/transformer` 与 v1/v2 运行时里也有类似的格式转换设施，可以对照阅读，理解为何 OM2 选择自带一套独立、精简的实现（随 `om2_executor` 一起分发，依赖最小化）。
- **深入块格式语义**：若对 `FRACTAL_Z` / `FRACTAL_NZ` / `FRACTAL_ZZ` 等更复杂的块排布感兴趣，可阅读 `format_transfer_fractal_z.cc`、`format_transfer_fractal_nz.cc`，它们与本讲的 `NCHW→NC1HWC0` 共用同一套抽象与注册机制，只是下标映射更复杂。
- **回到编译侧**：运行期的 reformat 对应编译期「算子需要某种块格式输入」的约束。建议接下来学习单元 5 的 shape 推导与单元 7 的内存规划，理解编译期如何决定「哪些张量需要、以何种格式存放」，从而在运行期触发本子系统的转换。
- **承接 u6-l6**：可重读 u6-l6 中 `Om2RTVarManager` 的变量搬运三路径（init_data / trans_road / copy_info），结合本讲确认 `trans_road` 路径的完整实现细节。
