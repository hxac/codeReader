# op_kernel 层：AscendC Kernel 入门

## 1. 本讲目标

前两讲我们走完了 op_host 的 OpDef 注册（u2-l1）与 Tiling 计算（u2-l3），上一讲结尾 aclnn 层已把算子登记进下发列表。本讲跨过 Host/Device 边界，进入算子真正执行计算的地方——op_kernel 层。

学完本讲，你应该能够：

1. 看懂 AscendC kernel 入口函数 `extern "C" __global__ __aicore__` 的参数布局，并说清每个参数从哪里来。
2. 解释 `GET_TILING_DATA` 如何把 host 侧算好的 TilingData「施工图」在 device 侧解包，以及 `TILING_KEY_IS` 如何让 kernel 对号入座。
3. 读懂 Kernel 类的 `Init` / `Process` 两段式结构，理解 `TPipe` 如何管理 UB 内存、`TQue` 双缓冲如何让「搬入」和「计算」流水重叠。
4. 能够仿照现有算子，独立写出一个新算子的 kernel 入口函数与空 Kernel 类骨架。

本讲仍以最小算子 `ai_infra_scatter_block_update` 为主要标本，并用 `ai_infra_mhc_sandwich_norm_post_preonly` 作对照，展示入口层的另一种写法。

## 2. 前置知识

本讲是全书第一次真正站在 NPU 硬件的视角看代码，先把几个概念补齐。

### 2.1 回顾：Host 只算计划，Device 只执行

u2-l3 已建立的心智模型：host 侧 Tiling 在算子执行前算出数据切分方案，打包成 TilingData 下发；kernel 拿着这份「施工图」干活。本讲要回答的正是：kernel 这一边，图纸是怎么送到手里的、又是怎么照着施工的。

### 2.2 AI Core、AI Vector 核与 blockIdx

昇腾 NPU 上有众多计算核心。对向量类算子，每个核是一个 AI Vector 核（AIV）。一次 kernel 启动会同时点亮一批核，所有核执行**同一份 kernel 代码**，靠 `GetBlockIdx()` 拿到自己的编号来区分「我处理哪一段数据」。这和 CUDA 的 thread block 思想类似：**代码一份，数据各管一段**。第 5 单元会详细讲 AIV 与立方核（AIC）的协同，本讲只需理解「多核各自领任务」。

### 2.3 GM 与 UB：两级内存

- **GM（Global Memory）**：device 上的全局内存（HBM），容量大、带宽相对低。算子的输入输出张量都放在这里。
- **UB（Unified Buffer）**：核内的高速缓存，容量小（本算子 host 侧默认按 192KB 预估），但计算单元只能直接访问它。所以数据必须先 `GM → UB`（搬入），算完再 `UB → GM`（搬出）。

负责在 GM 和 UB 之间搬运的是 MTE 搬运单元，其中 MTE2 负责 GM→UB 的搬入，MTE3 负责 UB→GM 的搬出。**搬运和计算由不同硬件单元负责，可以并行**——这是本讲 double buffer 流水的物理基础。

### 2.4 相关类型速查

| 概念 | 一句话解释 |
| --- | --- |
| `GM_ADDR` | 全局内存地址类型（构建系统/CANN 头文件提供），kernel 入口的每个张量参数都是它 |
| `__gm__` | 地址空间修饰符，标注指针指向 GM |
| `GlobalTensor<T>` | 指向 GM 中一段类型为 `T` 的数据的句柄，可带偏移下标访问 |
| `LocalTensor<T>` | 指向 UB 中一段数据的句柄，计算 API 的操作对象 |
| `TPipe` | UB 内存的管理者，负责给队列分配 buffer |
| `TQue` | UB 队列，配合「搬运/计算」两个阶段做同步 |
| MTE2 / MTE3 | GM→UB / UB→GM 的搬运通道 |
| AIV | AI Vector 向量核，本讲算子运行的核类型 |

`GM_ADDR`、`GET_TILING_DATA`、`TILING_KEY_IS` 这些宏的定义位于 CANN 安装目录的头文件（经 `kernel_operator.h` 引入），不在本仓库内，本讲按语义讲解、不给外部行号。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [ai_infra_scatter_block_update.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp) | 主标本的 kernel 入口函数（36 行，全书最短的入口之一） |
| [ai_infra_scatter_block_update.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h) | 主标本的 Kernel 类：Init/Process/CopyIn/ScatterOut 全部实现 |
| [ai_infra_scatter_block_update_tiling.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h) | host 侧 TilingData 字段定义，是 device 侧解包的「另一端契约」 |
| [ai_infra_scatter_block_update_tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp) | host 侧 TilingKey 的赋值处，与 kernel 侧对号 |
| [ai_infra_mhc_sandwich_norm_post_preonly.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp) | 对照标本：多参数入口、workspace 处理、tiling 下放 |
| [ai_infra_mhc_sandwich_norm_post_preonly_kernel.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h) | 对照标本：Init 内部解包 tiling 的写法 |

## 4. 核心概念与源码讲解

### 4.1 kernel 入口：`extern "C" __global__ __aicore__` 与参数布局

#### 4.1.1 概念说明

op_kernel 目录的 `.cpp` 文件里只有一个（或极少数）函数是「入口」——它就是运行时按名字找到并启动的 kernel。入口函数有三个标志性修饰：

- `extern "C"`：关闭 C++ 名字修饰（name mangling），保证函数符号名就是 `ai_infra_scatter_block_update` 这样的纯名字，运行时才能按符号名查找 kernel。**没有它，符号会变成带参数编码的长名，查找直接失败。**
- `__global__`：表示这是「grid 级」函数，一次启动会在多个核上各自执行一份。
- `__aicore__`：表示代码跑在 AI Core 上（而非控制核），编译器据此选择指令集与编译管线。

入口的**参数布局是固定契约**：先是 OpDef 中声明的全部输入、输出张量（按声明顺序），末尾固定追加 `workspace` 和 `tiling` 两个参数。上一讲 aclnn 层执行器下发的就是这串地址。

还有一个隐式契约：入口里出现的 `DTYPE_INPUT`、`DTYPE_INDICES` 不是 C++ 类型，而是**编译期类型宏**。u2-l1 讲过 OpDef 中 Input 声明了支持的数据类型组合（如 FP16/INT32、BF16/INT64），编译系统会为每种组合实例化一份 kernel 代码，把宏替换成具体类型。文件头注释明确说明了这一点：

> DTYPE_INPUT / DTYPE_INDICES are compile-time type macros provided by the build system based on OpDef input declarations.

#### 4.1.2 核心流程

入口函数的通用骨架（伪代码）：

```text
extern "C" __global__ __aicore__ void 算子名(
    GM_ADDR 输入1, GM_ADDR 输入2, ..., GM_ADDR 输出1, ...,   ← OpDef 声明顺序
    GM_ADDR workspace,                                       ← 固定倒数第二
    GM_ADDR tiling)                                          ← 固定最后
{
    （可选）声明任务类型 KERNEL_TASK_TYPE_DEFAULT(...)
    （可选）workspace 判空与 GetUserWorkspace 换算
    GET_TILING_DATA(本地名, tiling);                         ← 解包施工图
    TPipe pipe;                                              ← UB 内存管理者
    if (TILING_KEY_IS(某key)) {                              ← 对号入座
        XxxKernel<DTYPE_输入, ...> op;                       ← 类型宏在此生效
        op.Init(所有 GM 地址, tiling, &pipe);
        op.Process();
    }
}
```

#### 4.1.3 源码精读

**主标本入口**——[ai_infra_scatter_block_update.cpp:L25-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L25-L35)：kernel 入口。参数依次是 `input`、`indices`、`update`、`input_out` 四个张量地址（顺序与 OpDef 的 Input/Output 声明一致；本算子是原地算子，`input_out` 即输出），随后固定跟 `workspace` 与 `tiling`。函数体只有四步：解包 tiling → 建 TPipe → 校验 TilingKey → 实例化 Kernel 类并 Init/Process。

[ai_infra_scatter_block_update.cpp:L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L31)：`ScatterBlockUpdateKernel<DTYPE_INPUT, DTYPE_INDICES> op;` 模板实参就是两个类型宏——同一段源码被编译成 FP16/INT32、BF16/INT64 等多份代码。**类型分派发生在编译期，运行期零开销。**

注意：本算子入口收了 `workspace` 参数却完全没用它——参数布局是统一契约，不用也得收。host 侧此算子的 `GetWorkspaceSize` 直接返回成功不申请空间，两者是配套的。

**对照标本入口**——[ai_infra_mhc_sandwich_norm_post_preonly.cpp:L25-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L25-L45)：MHC 算子入口，展示了三个新要素：

1. [L26-L29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L26-L29)：13 个张量参数 + `workspace` + `tiling`，参数再多也遵循同一布局规则。
2. [L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L31)：`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY);` 声明这是纯向量核任务，框架据此做任务调度（对照：scatter 入口没写，走默认）。
3. [L33-L39](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L33-L39)：对 `workspace` 判空后调用 `GetUserWorkspace(workspace)` 换算出用户 workspace 的起始地址——系统 workspace 与用户 workspace 在同一块内存的不同区段，需要这个换算才能安全使用（该算子用 workspace 做核间数据交换）。

另外，MHC 的 tiling 没有在入口解包，而是把原始指针直接传给 `op.Init(...)`，在 Init 内部才 `GET_TILING_DATA`（见 4.2.3）。两种写法都合法，区别只是入口层的薄厚。

MHC kernel 目录还示范了**多文件组织**：[ai_infra_mhc_sandwich_norm_post_preonly.cpp:L14-L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L14-L20) 的注释列出了分工——入口 `.cpp`、公共常量 `_common.h`、类骨架 `_kernel.h`、双核/多 tile/单核三条实现路径各一个头文件。当 kernel 逻辑庞大时，这是本仓库的推荐拆法（第 4、5 单元会大量见到）。

#### 4.1.4 代码实践

**实践目标**：不看任何资料，徒手写出 kernel 入口签名。

**操作步骤**：

1. 打开 [ai_infra_scatter_block_update_def.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_def.cpp)（u2-l1 读过的 OpDef），数一遍 Input/Output 的声明顺序。
2. 合上文件，凭记忆在纸上默写 `ai_infra_scatter_block_update` 的入口签名。
3. 与 [ai_infra_scatter_block_update.cpp:L25-L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L25-L26) 逐参数对照。
4. 再挑战高难度：数 MHC 的 OpDef（`ai_infra_mhc_sandwich_norm_post_preonly_def.cpp`）的 IO 数量，与 [ai_infra_mhc_sandwich_norm_post_preonly.cpp:L26-L29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp#L26-L29) 对照。

**需要观察的现象**：入口参数与 OpDef 声明顺序严格一一对应；参数总数 = IO 数 + 2（workspace、tiling）。

**预期结果**：两次默写全部命中。这是纯阅读实践，无需硬件，**可直接完成**。

#### 4.1.5 小练习与答案

**练习 1**：为什么入口函数必须加 `extern "C"`？去掉会发生什么？

> **答案**：`extern "C"` 关闭 C++ 的名字修饰。去掉后编译出的符号会变成含参数类型编码的长名（如 `_Z29ai_infra_scatter_block_updatePKvS_S_...`），而运行时按 `ai_infra_scatter_block_update` 这样的裸名字查找 kernel，符号对不上，算子无法被启动。

**练习 2**：scatter 入口收了 `workspace` 参数却没用，为什么不能干脆删掉？

> **答案**：入口参数布局（IO 顺序 + workspace + tiling）是编译系统、aclnn 执行器与 kernel 三方共同的固定契约，所有算子统一。单个算子不用 workspace 也必须保留占位参数，否则参数错位，后续所有地址全部读错。

**练习 3**：`ScatterBlockUpdateKernel<DTYPE_INPUT, DTYPE_INDICES>` 中的两个宏是什么时候被确定成具体类型的？

> **答案**：编译期。构建系统读取 OpDef 中 Input 声明的数据类型组合，为每种组合生成一份编译实例并把宏替换为具体类型。所以 BF16 与 FP16 版 kernel 是两份独立二进制，运行时按张量实际 dtype 选用，没有运行期类型判断开销。

### 4.2 GET_TILING_DATA 与 TILING_KEY_IS：解包施工图与对号入座

#### 4.2.1 概念说明

上一讲讲到，host 侧 Tiling 类在 `PostTiling` 里把 TilingData 用 `SaveToBuffer` 序列化后随任务下发。到了 device 侧，需要两样东西：

- **`GET_TILING_DATA(名字, tiling)`**：把 `tiling` 指针指向的 GM 内存，按 host 侧定义的 TilingData 布局解包成本地结构体变量 `名字`。它是 u2-l3 讲过的「host/device 序列化契约」的 device 侧收货端：字段名、顺序、类型与 op_host 侧 `BEGIN_TILING_DATA_DEF` 中定义的逐一对应。
- **`TILING_KEY_IS(key)`**：判断本次下发的 TilingKey 是否等于 `key`。host 侧可能注册了多个 tiling 模板/多个 kernel 变体，编译后它们住在**同一个 kernel 符号**里，靠 TilingKey 区分——host 算出 key 随任务下发，kernel 用 `TILING_KEY_IS` 对号入座，只走匹配的那个分支。

一句话：**TilingData 告诉 kernel「数据怎么切」，TilingKey 告诉 kernel「你是哪份代码」**。

#### 4.2.2 核心流程

完整闭环（承接 u2-l3 的七步框架）：

```text
Host 侧                                    Device 侧
─────────                                  ─────────
DoOpTiling 算出切分参数
    │
GetTilingKey() 返回 tilingKey_ ────────►  TILING_KEY_IS(key) 命中哪个分支
    │（框架 SetTilingKey 落账）                │
PostTiling: SaveToBuffer 把         ───►  GET_TILING_DATA(t, tiling) 解包
  TilingData 序列化写入 tiling 区域            │
    │                                        t.xxx 逐字段可用
    │                                        Kernel Init/Process 照图施工
```

TilingKey 与 TilingData 的数值必须**双侧硬编码一致**，两侧各定义一次常量（host 是 `constexpr`，kernel 是 `#define`），这是最容易踩的坑：改了一侧忘了另一侧，kernel 会静默空跑。

#### 4.2.3 源码精读

**kernel 侧三行**——[ai_infra_scatter_block_update.cpp:L23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L23)：kernel 侧定义 `#define FULL_LOAD_TILING_KEY 1000`。[L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L28)：`GET_TILING_DATA(tilingData, tiling)` 解包出本地变量 `tilingData`。[L30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.cpp#L30)：只有 TilingKey 等于 1000 才实例化并执行 Kernel 类；不匹配则整个入口空跑（返回，什么都不做）。

**host 侧对端**——[ai_infra_scatter_block_update_tiling.cpp:L59](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L59)：host 侧同样定义 `FULL_LOAD_TILING_KEY = 1000`，数值与 kernel 侧 `#define` 必须一致。[L350](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L350)：`DoOpTiling` 尾部把 `tilingKey_` 赋值为 `FULL_LOAD_TILING_KEY`。[L371-L374](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L371-L374)：`GetTilingKey()` 把它交还给框架，由框架 `SetTilingKey` 落账随任务下发——这正是 u2-l3「TilingKey 是分支暗号」的出票端。

> 注意区分：[L78](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L78) `REGISTER_TILING_TEMPLATE(..., 1000)` 的 1000 是**注册优先级**（多 tiling 类轮询用，见 u2-l3），与 TilingKey 的 1000 数值相同纯属本算子只有一个模板的巧合，两者语义不同，别混为一谈。

**契约的另一端（TilingData 字段）**——[ai_infra_scatter_block_update_tiling.h:L25-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L25-L41)：`BEGIN_TILING_DATA_DEF` 与 `TILING_DATA_FIELD_DEF` 共声明 15 个字段。kernel 侧 `GET_TILING_DATA` 解包后访问的 `tilingData.eachCoreIndexCount` 等字段，名字就来自这里——你在 kernel 里能点出哪些字段，完全由这份 host 侧定义决定。

**MHC 的另一种时序**——[ai_infra_mhc_sandwich_norm_post_preonly_kernel.h:L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L41)：`GET_TILING_DATA(td, tiling)` 写在 Init 函数体内，[L43-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L43-L65) 随即把 20 多个 tiling 字段读进成员变量。同时注意：MHC 入口**没有** `TILING_KEY_IS`——它的多路径分派不走 TilingKey，而是依据 TilingData 里的 `coresPerToken` 等字段在类内选择单核/双核路径（[L75-L104](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L75-L104)）。两种分派手段（TilingKey 编译期分派 vs TilingData 字段运行期分派）在本仓库都常见，第 4 单元会看到更复杂的组合。

#### 4.2.4 代码实践

**实践目标**：亲手验证「双侧契约」并统计字段消费率。

**操作步骤**：

1. 打开 [ai_infra_scatter_block_update_tiling.h:L25-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L25-L41)，抄下 15 个字段名做成两列表格。
2. 打开 [ai_infra_scatter_block_update.h:L74-L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L74-L83)，把 kernel 实际读取的每个 `tiling.xxx` 在表格里打勾。
3. 统计哪些字段 kernel 从未读取，对每个未消费字段在 host 侧 tiling.cpp 中找到它的赋值处，思考它为谁而设（提示：有的字段供 host 侧校验或打印 `PrintTilingData` 用，见 [ai_infra_scatter_block_update_tiling.cpp:L360-L368](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.cpp#L360-L368)）。

**需要观察的现象**：15 个字段中 kernel 只消费一部分；TilingData 是「host 的完整工作记录」，不只是「kernel 的图纸」。

**预期结果**：kernel 读取 9 个字段（`updateDimSize`、`eachCoreIndexCount`、`tailCoreIndexCount`、`usedCoreNum`、`maxIndicesPerLoad`、`inputStride0`、`inputStride1`、`oneIndexSize`、`oneUpdateAlignSize`），其余 6 个（`totalIndicesCount`、`totalCoreNum`、`ubSize`、`indicesPerLoad`、`indicesTypeSize`、`updateTypeSize`）在 kernel 中未消费。本实践为纯阅读实践，**可直接完成**。

#### 4.2.5 小练习与答案

**练习 1**：如果有人把 kernel 侧的 `FULL_LOAD_TILING_KEY` 从 1000 改成 1001，host 侧不动，会发生什么？

> **答案**：host 下发的 TilingKey 仍是 1000，kernel 侧 `TILING_KEY_IS(1001)` 恒为假，入口直接空跑——算子「执行成功」但没有任何数据被写入，输出错误或维持原值，且不报错。这类静默失败极难排查，所以双侧常量必须同改。

**练习 2**：`GET_TILING_DATA(tilingData, tiling)` 之后，`tilingData.eachCoreIndexCount` 这个字段名是在哪个文件里定义的？kernel 目录里为什么找不到它的定义？

> **答案**：定义在 op_host 侧的 [ai_infra_scatter_block_update_tiling.h:L30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_host/ai_infra_scatter_block_update_tiling.h#L30)（`TILING_DATA_FIELD_DEF(int64_t, eachCoreIndexCount)`）。`GET_TILING_DATA` 宏（CANN 头文件提供）在编译期根据 host 侧生成的布局信息展开出解包代码，kernel 源码里只写字段访问，不需要也看不到字段定义——这正是「一份定义、两端使用」的序列化契约。

**练习 3**：MHC 算子为什么可以不用 `TILING_KEY_IS`？

> **答案**：TilingKey 分派适用于「一份 kernel 源码里编译进多个变体、按 key 选择」的场景。MHC 的单核/双核等路径差异由 TilingData 字段（如 `coresPerToken`）描述，kernel 在 Init 里读字段做运行期分派即可，不需要为每条路径分配独立 TilingKey。两种机制解决同一个问题（多形态算子），选择取决于变体数量与是否需要编译期隔离。

### 4.3 Kernel 类设计：Init/Process 两段式与 TPipe 内存管理

#### 4.3.1 概念说明

入口函数故意写得很薄，真正的工作交给一个 **Kernel 类模板**。本仓库的通用范式：

- **`Init(...)`（备料）**：把 GM 地址绑定为 `GlobalTensor`；把 tiling 字段读进成员变量；调用 `pipe->InitBuffer(...)` 为每个队列在 UB 上分配缓冲区。**只做一次性的准备工作。**
- **`Process()`（施工）**：`GetBlockIdx()` 领任务；按 tiling 给出的切分范围循环「搬入 → 计算/搬出」，直到本核数据处理完。

**TPipe 是 UB 的唯一管理者**。核内 UB 空间有限（本算子 host 侧默认按 192KB 预估），所有队列的缓冲区都从它这里划拨：`InitBuffer(que, bufNum, bytes)` 给队列 `que` 分配 `bufNum` 个、每个 `bytes` 大小的 buffer。超出 UB 总量的分配会在编译/运行期失败，所以 host 侧 tiling 计算 `maxIndicesPerLoad`（一次最多搬多少行）本质上就是在解「UB 容量约束下的批量大小」方程。

**TQue 队列驱动流水**。队列把「搬入」和「消费」两个阶段解耦：

- 生产端（CopyIn）：`AllocTensor()` 要一块空闲 buffer → `DataCopyPad` 搬数据 → `EnQue` 标记就绪；
- 消费端（ScatterOut）：`DeQue` 等待就绪并取出 → 处理/搬出 → `FreeTensor` 归还 buffer。

当队列有 2 个 buffer（double buffer）时，生产端往 slot A 搬第 N+1 批数据的同时，消费端可以处理 slot B 里的第 N 批——MTE2 搬运与计算自然重叠，这就是 `SCATTER_BUF_NUM = 2` 的意义。

#### 4.3.2 核心流程

一次 `Process()` 的执行流程（以本核分到 `coreCount` 行索引为例）：

```text
GetBlockIdx() = b；若 b >= usedCoreNum 直接返回（多启动的核闲转）
coreStart = b × eachCoreIndexCount
coreCount = (b 是最后一核) ? tailCoreIndexCount : eachCoreIndexCount
while 还有剩余:
    loadCount = min(剩余, maxIndicesPerLoad)     ← 一批塞得进 UB 的量
    CopyIn(coreStart + processed, loadCount)      ← GM→UB，MTE2
    ScatterOut(loadCount)                         ← UB→GM，含标量读索引
```

ScatterOut 内部对每行索引 `i`：读出 `(idx0, idx1)` → 按 `offset = idx0 × stride0 + idx1 × stride1` 算出 GM 目标行 → 把 `updLocal` 的第 `i` 行整行 `DataCopyPad` 写回 GM。

double buffer 的收益可以形式化：设一批数据搬运耗时 \( t_{copy} \)、计算耗时 \( t_{calc} \)，串行执行 \( n \) 批的总时间为

\[
T_{serial} = n \,(t_{copy} + t_{calc})
\]

双缓冲下搬运与计算重叠，稳态每批耗时取两者较大值：

\[
T_{pipe} \approx t_{copy} + (n-1)\,\max(t_{copy},\, t_{calc}) + t_{calc}
\]

当 \( n \) 较大且 \( t_{copy} \approx t_{calc} \) 时，\( T_{pipe} \approx n \max(t_{copy}, t_{calc}) \)，相比串行接近减半。这就是注释里「MTE2 与 MTE3 自然重叠」的数学含义。

本算子的核心循环（每行 i 的散写）在数学上就是：

\[
\text{input}[\text{idx}_0^{(i)},\ \text{idx}_1^{(i)},\ :] \leftarrow \text{update}[i,:], \quad i = 0,1,\dots,T-1
\]

`input` 支持 dim0 非连续（stride 保留），所以目标地址要按 stride 折算，而不是简单的行号乘行宽。

#### 4.3.3 源码精读

**类骨架**——[ai_infra_scatter_block_update.h:L29-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L29-L58)：Kernel 类声明。两个模板参数 `T`（数据类型）与 `IndexT`（索引类型）由入口的 `DTYPE_INPUT/DTYPE_INDICES` 宏传入；公有接口只有 `Init` 与 `Process` 两个（[L32-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L32-L35)），私有的 `CopyIn/ScatterOut`（[L38-L39](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L38-L39)）恰好对应「搬入」「搬出」两个流水阶段。成员分四组：三个 `GlobalTensor`（GM 只读句柄，L41-L43）、两个 `TQue`（UB 队列，L45-L46）、`TPipe*`（L47）、以及从 tiling 预热好的一批 int64 参数（L49-L57）。注意 `Init` 自己也是模板函数（`TilingDataT`），让类不绑定具体 TilingData 类型。

**Init 备料**——[ai_infra_scatter_block_update.h:L64-L94](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L64-L94)：三步备料。[L69-L71](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L69-L71) 把裸的 `GM_ADDR`（先 `reinterpret_cast` 成 `__gm__ T*`）绑定为类型安全的 `GlobalTensor`，此后下标访问、偏移计算都带类型。[L74-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L74-L84) 把 tiling 字段读进成员（其中 `updateRowElements_` 由 `oneUpdateAlignSize_ / sizeof(T)` 派生——UB 中每行对齐后的元素数）。[L88-L93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L88-L93) `pipe_->InitBuffer` 给两个队列各划 2 个 buffer：indices 队列每块 `maxIndicesPerLoad_ × oneIndexSize` 字节，update 队列每块 `maxIndicesPerLoad_ × oneUpdateAlignSize` 字节——**批量上限与 UB 划拨在这里闭环**（这两个值都是 host 侧 tiling 按 192KB UB 算出来的）。

**Process 分核施工**——[ai_infra_scatter_block_update.h:L97-L124](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L97-L124)：[L99-L102](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L99-L102) 每个核先问自己的编号，编号超出 `usedCoreNum` 的核立即返回（框架启动的核数可能多于实际用量）。[L104-L107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L104-L107) 按本核编号算出负责的索引区间：普通核领 `eachCoreIndexCount` 个，**最后一核领 `tailCoreIndexCount` 个**（总数除不尽时余数全给尾核，这就是 tiling 留两个分核字段的原因）。[L113-L123](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L113-L123) 注释清楚解释了 double buffer 的队列机制，主循环每轮取 `min(剩余, maxIndicesPerLoad)` 行，CopyIn → ScatterOut → 推进游标。

**CopyIn 搬入**——[ai_infra_scatter_block_update.h:L127-L156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L127-L156)：[L130-L131](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L130-L131) 从两个队列各 `AllocTensor` 一块空闲 buffer。[L134-L141](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L134-L141) 用 `DataCopyPad` 把 `loadCount` 对索引从 GM 搬进 UB，`DataCopyExtParams` 描述搬运形状（blockCount/blockLen/stride），`DataCopyPadExtParams{false,...}` 表示不做取反/padding 修饰。[L144-L152](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L144-L152) 搬 update：每行在 UB 里按 `dstStride=0` 的 padding 布局落到对齐后的行距上——注释点明目的：保证 ScatterOut 取第 `i` 行子视图时偏移满足 32 字节对齐（昇腾 MTE 搬运的对齐要求）。[L154-L155](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L154-L155) `EnQue` 把两块 buffer 标记就绪。

**ScatterOut 消费**——[ai_infra_scatter_block_update.h:L159-L191](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L159-L191)：[L163-L164](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L163-L164) `DeQue` 等待 MTE2 搬运完成并取出 buffer。[L165-L167](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L165-L167) 申请并立即等待一个 `HardEvent::MTE2_S` 事件——因为下面 [L170-L171](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L170-L171) 要用 `GetValue` **逐个标量读**索引值喂给控制流，标量读必须显式等 MTE2 搬运落盘。[L172-L174](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L172-L174) 负索引直接跳过（防御非法输入）。[L177-L178](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L177-L178) 按保留的 stride 折算 GM 目标行偏移（对应 u2-l2 讲过的 `CreateView` 原地语义——stride 一路从 aclnn 层传到 kernel）。[L182-L187](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L182-L187) 用 `DataCopyPad` 把 UB 中第 `i` 行整行写回 `inputGm_[gmOffset]`。[L189-L190](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h#L189-L190) `FreeTensor` 归还两块 buffer，供下一轮 CopyIn 使用。

**MHC 对照：workspace 也是流水线资源**——MHC 的 Init（[ai_infra_mhc_sandwich_norm_post_preonly_kernel.h:L67-L104](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly_kernel.h#L67-L104)）在常规备料之外，还按核编号为每个核（或每对双核）在 workspace 上划分专属区段（`pairWsBase_`），用于双核协同时的数据交换——这预告了第 5 单元的跨核同步主题。

#### 4.3.4 代码实践

**实践目标**：吃透一次 `Process` 调用的数据流，并完成 my_add 的 kernel 侧骨架。

**操作步骤（第一部分：画数据流图）**：

1. 通读 [ai_infra_scatter_block_update.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/index/ai_infra_scatter_block_update/op_kernel/ai_infra_scatter_block_update.h)，列出 Kernel 类的模板参数与全部成员变量，标注每个成员属于「GM 句柄 / UB 队列 / tiling 参数」哪一类。
2. 在纸上画一张数据流图，必须包含：`indicesGm_`/`updateGm_`（GM）→（DataCopyPad/MTE2）→ `indicesQue_`/`updateQue_`（UB slot A/B 交替）→（GetValue 标量读 + DataCopyPad/MTE3）→ `inputGm_`（GM）。在箭头上标注每一步用到的关键 API（`AllocTensor`/`EnQue`/`DeQue`/`FreeTensor`/`SetFlag`/`WaitFlag`）。
3. 用红笔圈出图中所有「同步点」（EnQue、DeQue、SetFlag/WaitFlag），并各写一句话说明它防止什么竞态。

**需要观察的现象**：数据只在 GM 和 UB 之间往返，计算单元从不直接碰 GM；队列 API 严格成对出现（Alloc↔Free、EnQue↔DeQue）。

**预期结果**：得到一张 GM→UB→GM 的完整数据流图。此部分为纯阅读实践，**可直接完成**。

**操作步骤（第二部分：写 my_add 骨架）**：见第 5 节综合实践。

#### 4.3.5 小练习与答案

**练习 1**：`CopyIn` 里 `AllocTensor` 之后如果没有调用 `EnQue`，程序会怎样？

> **答案**：消费端 `DeQue` 永远等不到就绪标记，流水卡死（死等）。队列语义要求严格配对：`AllocTensor` 领用 buffer → `EnQue` 标记就绪 → `DeQue` 取出 → `FreeTensor` 归还。少任何一环，生产与消费的握手就断了。

**练习 2**：`ScatterOut` 已经 `DeQue` 了，为什么还要 `SetFlag/WaitFlag<HardEvent::MTE2_S>`？

> **答案**：`DeQue` 保证队列层面的时序（这块 buffer 已被标记就绪），但后面 `GetValue` 是**标量直接读 UB 内存**。MTE2 搬运是异步的，标量读需要显式的硬件事件依赖（MTE2→Scalar）确保数据真正落盘。没有这对 Set/Wait，可能读到旧值，索引算错导致写错 GM 位置。

**练习 3**：把 `SCATTER_BUF_NUM` 从 2 改成 1，功能还正确吗？性能会怎么变？

> **答案**：功能仍然正确——队列语义在单 buffer 下照样成立。但 CopyIn 必须等 ScatterOut `FreeTensor` 归还唯一的 buffer 后才能开始下一批，MTE2 搬运与计算无法重叠，流水退化成串行，总耗时从 \( \approx n\max(t_{copy},t_{calc}) \) 退回 \( n(t_{copy}+t_{calc}) \)。

**练习 4**：为什么最后一核领的任务量要用单独的 `tailCoreIndexCount`，而不是所有核都用 `eachCoreIndexCount`？

> **答案**：总索引数 T 未必能被核数整除。若统一按 `ceil(T/n)` 分配，前 n-1 核按此量领完后剩余量会小于该值，尾核只能领到余量。用两个字段分别记录「常规核任务量」与「尾核任务量」，才能保证各核任务区间首尾相接、不重不漏地覆盖 [0, T)。

## 5. 综合实践

**综合实践目标**：为假想的 `my_add` 算子（`z = x + y`，两个同形状输入、一个输出）写出 kernel 侧的入口函数与空 Kernel 类骨架，把本讲三个模块（入口契约、tiling 解包、类结构）全部串起来。

**任务清单**：

1. **设计入口签名**。设 OpDef 声明为 Input(x)、Input(y)、Output(z)，则入口为三个张量地址 + workspace + tiling 共 5 个参数。data type 用 `DTYPE_X`（假设两种输入同类型）。
2. **写入口函数**（示例代码，不是仓库原有文件）：

```cpp
// ===== 示例代码：my_add 的 kernel 入口（仿照 ai_infra_scatter_block_update.cpp）=====
#include "kernel_operator.h"
#include "my_add.h"

using namespace AscendC;

#define MY_ADD_TILING_KEY 1000   // 必须与 host 侧 tiling.cpp 中的常量一致

extern "C" __global__ __aicore__ void my_add(
    GM_ADDR x, GM_ADDR y, GM_ADDR z, GM_ADDR workspace, GM_ADDR tiling)
{
    GET_TILING_DATA(tilingData, tiling);   // 解包施工图
    TPipe pipe;                             // UB 内存管理者
    if (TILING_KEY_IS(MY_ADD_TILING_KEY)) { // 对号入座
        MyAddKernel<DTYPE_X> op;            // 类型宏在编译期定型
        op.Init(x, y, z, tilingData, &pipe);
        op.Process();
    }
}
```

3. **写空 Kernel 类骨架**（示例代码，放在 `my_add.h`）：

```cpp
// ===== 示例代码：my_add 的空 Kernel 类骨架（仿照 ScatterBlockUpdateKernel）=====
#include "kernel_operator.h"

using namespace AscendC;

constexpr int32_t MY_ADD_BUF_NUM = 2;   // double buffer

template <typename T>
class MyAddKernel {
public:
    template <typename TilingDataT>
    __aicore__ inline void Init(GM_ADDR x, GM_ADDR y, GM_ADDR z,
                                const TilingDataT& tiling, TPipe* pipe)
    {
        // 第一步：绑定 GlobalTensor（reinterpret_cast<__gm__ T*> 后 SetGlobalBuffer）
        // 第二步：把 tiling 的 totalElemNum / usedCoreNum / eachCoreElemCount 等读进成员
        // 第三步：pipe->InitBuffer(...) 为 xQue_/yQue_/zQue_ 划拨 UB（字节数按批量上限算）
    }
    __aicore__ inline void Process()
    {
        // GetBlockIdx 领区间 → while 循环 { CopyIn 一批; Add 并写出一批 }
    }
private:
    __aicore__ inline void CopyIn(int64_t start, int64_t count) { /* Alloc→DataCopy→EnQue */ }
    __aicore__ inline void AddOut(int64_t count)                { /* DeQue→计算→DataCopy→Free */ }

    GlobalTensor<T> xGm_, yGm_, zGm_;
    TQue<TPosition::VECIN,  MY_ADD_BUF_NUM> xQue_, yQue_;   // 搬入队列
    TQue<TPosition::VECOUT, MY_ADD_BUF_NUM> zQue_;          // 搬出队列
    TPipe* pipe_ = nullptr;
    int64_t eachCoreElemCount_ = 0;
    int64_t tailCoreElemCount_ = 0;
    int32_t usedCoreNum_ = 0;
    int64_t elemsPerLoad_ = 0;
};
```

4. **自查清单**（对照本讲三个模块逐条打勾）：
   - 入口有 `extern "C" __global__ __aicore__`；参数顺序 = OpDef IO 顺序 + workspace + tiling。
   - `GET_TILING_DATA` 在入口（或 Init 内，二选一）；kernel 侧 TilingKey 常量与 host 侧数值一致。
   - 类成员四件套齐全：GlobalTensor、TQue、TPipe*、tiling 参数；队列 API 全部成对。
5. **验证方式**：本实践为源码阅读型实践，不要求上机。如需进一步验证，可对照 u6-l3 的九件套清单把 my_add 补成完整算子后用 `bash build.sh -n 'my_add'` 走编译链路静态检查——**待本地验证**（需要昇腾 Docker 环境与 CANN 工具链）。

**预期结果**：一份能通过自查清单的 `my_add.cpp` + `my_add.h` 骨架，以及 4.3.4 画出的 scatter 数据流图。两者合起来，就是你未来阅读仓库里任何一个 kernel 目录的「对照模板」。

## 6. 本讲小结

- **kernel 入口是三方契约**：`extern "C" __global__ __aicore__` 修饰 + 「OpDef IO 顺序 + workspace + tiling」的固定参数布局；`DTYPE_*` 是编译期类型宏，由构建系统按 OpDef 数据类型组合实例化出多份 kernel。
- **GET_TILING_DATA 是 u2-l3 序列化契约的收货端**：host 侧 `BEGIN_TILING_DATA_DEF` 定义字段、`PostTiling` 的 `SaveToBuffer` 写入，device 侧一键解包，字段名两端一致。
- **TilingKey 是分支暗号，且必须双侧硬编码一致**：host `DoOpTiling` 赋 `tilingKey_` → `GetTilingKey` 交框架落账 → kernel `TILING_KEY_IS` 对号入座；两侧数值不一致会静默空跑。MHC 展示了替代方案——按 TilingData 字段运行期分派。
- **Kernel 类两段式**：`Init` 绑 GM、读 tiling、`InitBuffer` 划 UB（一次性备料）；`Process` 用 `GetBlockIdx` 领区间、按批循环 CopyIn→消费（尾核任务量单独用 `tailCoreIndexCount` 兜住余数）。
- **TPipe 管 UB、TQue 驱动流水**：`AllocTensor/EnQue/DeQue/FreeTensor` 严格成对；double buffer 让 MTE2 搬运与计算重叠，总耗时从串行的 \( n(t_{copy}+t_{calc}) \) 降到约 \( n\max(t_{copy},t_{calc}) \)。
- **同步点有两层**：队列 API 管生产/消费握手，`SetFlag/WaitFlag<HardEvent::MTE2_S>` 管标量读 UB 前的搬运落盘依赖，缺一不可。

## 7. 下一步学习建议

本讲结束后，op_kernel 层的「单算子骨架」你已经掌握。建议按以下顺序继续：

1. **补上 op_host 的最后一块拼图**：若你还未读 u2-l5（InferShape 与 proto），建议先完成它，至此一个算子目录的五件套你就全部见过了。
2. **横向再读一个 kernel 目录**：推荐 [ai_infra_mhc_sandwich_norm_post_preonly/op_kernel](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/mhc/ai_infra_mhc_sandwich_norm_post_preonly/op_kernel/ai_infra_mhc_sandwich_norm_post_preonly.cpp) 的全部头文件（common/kernel/dualcore/singlecore），体会「一个类、多条实现路径」的多文件组织，为第 4 单元的大算子热身。
3. **进入第 3 单元**：u3-l1 起，我们从 device 回到 host 的 PyTorch 侧，讲 torch_ops_extension 如何用 `TORCH_LIBRARY` 注册算子签名——学完即可把「Python 调用 → aclnn → tiling → 本讲的 kernel」全链路闭环。
4. **提前埋个钩子**：本讲看到的 `GetUserWorkspace` 与 MHC 双核按 workspace 区段交换数据，是第 5 单元「AIV/AIC 协同与跨核同步」的入口，届时再深入。
