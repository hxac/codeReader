# 枚举与硬件位置：TPosition、HardEvent 与芯片概念

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `TPosition` 每个成员的含义，并解释它与 `Hardware`（物理存储层次）的区别与映射关系。
2. 解释 `HardEvent` 的命名规则「生产者_消费者」，并说明 `MTE2_V`、`V_MTE3`、`MTE3_MTE2` 三个方向分别保护哪条数据依赖。
3. 读懂 Add 示例中 `BUFFER_NUM=2` 双缓冲流水线的完整同步链，回答「为什么 `set_flag` 和 `wait_flag` 总是成对出现、甚至紧挨着出现」。
4. 跟踪一个枚举值从 Python `IntEnum` 到 IR 属性、再到 Ascend C 模板参数的完整传递路径。

## 2. 前置知识

本讲建立在前两讲（u1-l4 Add 示例、u2-l2 Tensor 抽象）之上，先回顾并补充几个概念。

### 2.1 流水线（Pipe）：AI Core 内部的异步执行单元

第一讲提过「MTE2 搬入、V 计算、MTE3 搬出三条异步并发流水线」。这里把直觉补全：

- 昇腾 AI Core 内部有多个**硬件队列**（pipe），例如负责数据搬入的 MTE2、负责矢量计算的 V、负责数据搬出的 MTE3。
- 核函数的指令流被**按序分发**到各个队列，但各个队列**各自异步执行**。
- 因此「代码里 `data_copy` 写在 `add` 前面」并不保证搬入先于计算完成——跨队列的读写依赖必须显式同步。

`python/asc/language/core/enums.py` 中的 `PipeID` 枚举就是这些队列的编号清单（PIPE_S、PIPE_V、PIPE_M、PIPE_MTE1~MTE5、PIPE_FIX 等），本讲末尾的 `pipe_barrier` 类接口会用到它。

### 2.2 为什么需要「事件」

队列 A 生产数据、队列 B 消费数据时，需要一把跨队列的「锁」：A 做完了举旗（`set_flag`），B 开工前看旗（`wait_flag`）。旗子由两部分唯一确定：

- **事件方向**（`HardEvent`）：旗子从哪个队列举向哪个队列；
- **事件编号**（`event_id`）：同一方向可以有多面互不干扰的旗子，双缓冲正是靠它区分两块缓冲。

### 2.3 Python 的 IntEnum

`enums.py` 所有枚举都继承 `enum.IntEnum`：成员就是 `int`（`HardEvent.MTE2_V == 4`），可以直接传给需要整数的接口，也保留了名字便于阅读。这与 IR 侧的 `I32EnumAttr` 一一对应（见 4.2.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/asc/language/core/enums.py` | 全部前端枚举定义：`TPosition`、`HardEvent`、`Hardware`、`PipeID` 等 |
| `examples/01_add/add.py` | 本讲的运行载体：手动同步 + 双缓冲的完整示例 |
| `python/asc/language/basic/block_sync.py` | `set_flag`/`wait_flag` 等 Python 接口，把枚举送进 IR |
| `python/asc/language/core/tensor.py` | `LocalTensor` 构造函数如何消费 `TPosition` |
| `include/ascir/Dialect/Asc/IR/Core/Attributes.td` | 枚举在 ASC-IR 中的属性定义（`TPositionAttr`、`HardEventAttr`） |
| `include/ascir/Dialect/Asc/IR/Core/Tensor.td` | `LocalTensorV2Op`：TPosition 进入 IR 的入口 |
| `include/ascir/Dialect/Asc/IR/Basic/OpBlockSync.td` | `SetFlagOp`/`WaitFlagOp` 的 Op 定义 |
| `lib/Target/AscendC/Basic/BlockSync.cpp` | `WaitFlag` 的 Ascend C 代码发射（手写示例） |
| `lib/TableGen/include/Constant.h` | `paramTypeLists` 编码含义：哪个参数进模板、哪个进实参 |
| `python/asc/language/__init__.py` | 枚举如何被导出成 `asc.TPosition`、`asc.HardEvent` |

## 4. 核心概念与源码讲解

### 4.1 TPosition：队列与缓冲区的逻辑位置

#### 4.1.1 概念说明

`TPosition` 回答的问题是：**这个队列/缓冲区在数据流中扮演什么角色**。IR 侧属性定义对它的描述就是一个词组——"queue/buffer position"（队列/缓冲区位置）。

13 个成员可以分成三组：

| 分组 | 成员 | 含义 | 物理落点（对应 `Hardware`） |
| --- | --- | --- | --- |
| 全局 | `GM` | 全局内存 | GM |
| 矢量组 | `VECIN` / `VECOUT` / `VECCALC` | 搬入队列 / 搬出队列 / 计算用临时缓冲 | UB |
| 矩阵组 | `A1` `A2` `B1` `B2` `CO1` `CO2` | Cube 单元矩阵乘的左矩阵/右矩阵/输出在各级的缓冲 | L1、L0A/L0B、L0C |

注意 `TPosition`（逻辑角色）与 `Hardware`（物理存储层次）是两个枚举：前者描述「干什么的」，后者描述「放在哪」。三组矢量化成员都落在 UB 上；矩阵组成员分布在 L1/L0。这就是「逻辑位置 → 物理位置」的映射关系。

矢量算子（本讲的 Add）只用 `VECIN`/`VECOUT`/`VECCALC`；矩阵组留给高阶 API `Matmul`（u7-l1 会用到 `asc.TPosition.GM`、`CO1` 等）。

#### 4.1.2 核心流程

`TPosition` 在一次 JIT 编译中的旅程：

```text
Python: asc.LocalTensor(dtype, asc.TPosition.VECIN, addr, tile_size)
   │  （构造函数内 OverloadDispatcher 分发）
   ▼
ir.TPosition.symbolize(pos)  →  转成 IR 枚举属性
   ▼
builder.create_asc_LocalTensorV2Op(local_tensor 类型, pos属性, addr, tileSize)
   ▼
IR 文本: local_tensor_v2 带 vecin 属性的操作
   ▼
后续 Pass / Ascend C 发射依据该属性决定缓冲管理与同步方式
```

#### 4.1.3 源码精读

**枚举定义**——[python/asc/language/core/enums.py:248-261](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py#L248-L261) 定义了 `TPosition` 的全部 13 个成员；[python/asc/language/core/enums.py:124-133](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py#L124-L133) 定义物理存储层次 `Hardware`（GM/UB/L1/L0A/L0B/L0C/BIAS/FIXBUF）。两相对照即得 4.1.1 的映射表。

**LocalTensor 的位置约束**——[python/asc/language/core/tensor.py:199-202](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L199-L202) 的类文档写明：`LocalTensor` 用于存放 AI Core 内部存储数据，支持的 `TPosition` 为 VECIN、VECOUT、VECCALC、A1、A2、B1、B2、CO1、CO2——注意清单里**没有 GM**。

**构造函数消费 TPosition**——[python/asc/language/core/tensor.py:236-244](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L236-L244) 是 `LocalTensor` 四参构造的实现：调用 `ir.TPosition.symbolize(pos)` 把 Python 枚举符号化成 IR 属性，连同 `addr`、`tile_size` 一起交给 `create_asc_LocalTensorV2Op`。

**IR 侧的 Op 与属性**——[include/ascir/Dialect/Asc/IR/Core/Tensor.td:161-166](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L161-L166) 定义 `LocalTensorV2Op`：三个参数中 `pos` 是 `AscendC_TPositionAttr`（编译期属性），`addr`/`tileSize` 是 `UI32` 值。[include/ascir/Dialect/Asc/IR/Core/Attributes.td:396-414](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L396-L414) 用 `I32EnumAttr` 定义 `TPositionAttr`，每个成员配了小写字符串形式（`"vecin"`、`"vecout"`……），这是 IR 文本里的打印名。

**框架风格的对照**——[examples/02_add_framework/add_framework.py:39-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L39-L41) 中 `asc.TQue(asc.TPosition.VECIN, BUFFER_NUM)` 等三行说明：换成框架风格后，`TPosition` 依然是指定队列角色的同一套枚举，只是缓冲分配和同步交给了 `TPipe`/`TQue`（u2-l6 详讲）。

**导出路径**——[python/asc/language/__init__.py:204-237](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/__init__.py#L204-L237) 把 `TPosition`、`HardEvent`、`Hardware`、`PipeID` 等枚举从 `core/enums.py` 重新导出，再经 `asc/__init__.py` 的 `from .language import *` 进入顶层 `asc` 命名空间，所以用户代码写 `asc.TPosition.VECIN` 即可。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `TPosition` 以属性形式出现在 IR 里，并验证位置约束。

**操作步骤**：

1. 按 u1-l5 的方法设置 `PYASC_DUMP_PATH`，在 Model 模式下运行 `examples/01_add/add.py`。
2. 打开导出的 `codegen.mlir`，搜索 `local_tensor_v2`，找到三条实例。
3. 核对三条操作的 `pos` 属性分别是 `vecin`、`vecin`、`vecout`，与 [examples/01_add/add.py:45-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L45-L47) 中三个 `LocalTensor` 的构造参数一一对应。
4. 把 `add.py` 第 45 行 `x_local` 的位置改成 `asc.TPosition.VECCALC`（语义为计算临时缓冲），重新运行并再次 dump。

**需要观察的现象**：IR 中该操作的属性从 `vecin` 变为 `veccalc`。

**预期结果**：步骤 3 应看到两条 `vecin`、一条 `vecout`。步骤 4 中 `VECCALC` 属于 `LocalTensor` 支持清单，编译应能通过、结果应保持正确；`data_copy` 的文档也注明源/目的操作数支持 VECIN/VECCALC/VECOUT（见 [python/asc/language/adv/utils.py:2172-2173](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/utils.py#L2172-L2173) 的接口说明）。运行结果是否完全一致属于「待本地验证」——语义上输入缓冲用 VECCALC 不再向编译器表达「输入」角色，可能影响依赖后端位置推断的 Pass。

#### 4.1.5 小练习与答案

**练习 1**：能否写 `asc.LocalTensor(dtype, asc.TPosition.GM, 0, 1024)`？

**答案**：不能。`LocalTensor` 的文档明确支持清单不含 GM；GM 是 `GlobalTensor` 的世界，通过 `set_global_buffer` 绑定设备指针使用（u2-l2 讲过二段式创建）。

**练习 2**：`Hardware.UB` 和 `TPosition.VECIN` 是什么关系？

**答案**：`Hardware` 是物理存储层次（放在哪），`TPosition` 是逻辑角色（干什么用）。`VECIN` 搬入队列物理上分配在 UB；同理 `A1` 落在 L1、`CO2` 落在 L0C。前端 API 绝大多数场景只需指定逻辑位置。

**练习 3**：Add 示例为什么 x、y 用 `VECIN`，z 用 `VECOUT`，而不用三个 `VECCALC`？

**答案**：`VECIN`/`VECOUT` 向编译器表达了「输入队列/输出队列」的数据流方向。位置不只是注释——后端 Pass（如区分输入输出 Tensor 的处理）和自动同步分析都依赖它推断生产者/消费者关系。全用 `VECCALC` 虽可能仍能编译，但丢失了方向信息。

### 4.2 HardEvent：流水线间的事件同步

#### 4.2.1 概念说明

`HardEvent` 有 35 个成员，看似很多，其实全部由一条命名规则生成：

> **`生产者_消费者`**：`set_flag` 由「_」前的队列执行（举旗），`wait_flag` 在「_」后的队列上等待（看旗）。

例如 `MTE2_V`（值为 4）：MTE2 流水线搬完数据后 `set_flag`，V 流水线计算前 `wait_flag`。缩写词表：

| 缩写 | 硬件单元 | 职责 |
| --- | --- | --- |
| MTE2 | Memory Transfer Engine 2 | 搬入：GM/L1 → UB（`data_copy` 搬入走它） |
| MTE3 | Memory Transfer Engine 3 | 搬出：UB → GM |
| MTE1 | Memory Transfer Engine 1 | 搬入：GM → L1（Cube 场景） |
| V | Vector | 矢量计算（`asc.add` 等） |
| M | Matmul（Cube） | 矩阵乘 |
| FIX | Fixpipe | L0C → GM 的结果回写 |
| S | Scalar | 标量单元 |

Add 示例只用到三个方向，每个方向对应一条真实的数据依赖：

| 事件方向 | 保护的依赖 | 位置 |
| --- | --- | --- |
| `MTE2_V` | V 读 `x_local`/`y_local` 前，MTE2 必须搬完 | add.py:57-58 |
| `V_MTE3` | MTE3 搬出 `z_local` 前，V 必须算完 | add.py:63-64 |
| `MTE3_MTE2` | MTE2 覆写缓冲前，上一轮必须已搬出（详见 4.3） | add.py:68-69 |

一个初学者最常见的困惑：**为什么 `set_flag` 和 `wait_flag` 紧挨着写？这不是自己举旗自己看吗？** 不是。这两条指令会被分发到**不同的硬件队列**（`SetFlag<MTE2_V>` 进 MTE2 队列、`WaitFlag<MTE2_V>` 进 V 队列），各自在所在队列内按序执行。文本上的相邻只是「同一时刻提交了两条分别属于两个队列的指令」，真正的同步发生在硬件上两条队列之间。

#### 4.2.2 核心流程

以一轮迭代（`buf_id` 固定）为例，指令按提交顺序与实际执行关系如下：

```text
提交顺序（kernel 代码顺序）          实际所属队列        执行条件
────────────────────────────────────────────────────────────────
data_copy(x_local[buf], x_gm[...])   MTE2              无条件
data_copy(y_local[buf], y_gm[...])   MTE2              上一条完成后
set_flag(MTE2_V, buf)                MTE2              搬入完成后举旗
wait_flag(MTE2_V, buf)               V    ──等待旗──▶   MTE2 举旗后
add(z_local[buf], x_local[buf], ...) V                 旗到位后执行
set_flag(V_MTE3, buf)                V                 add 完成后举旗
wait_flag(V_MTE3, buf)               MTE3 ──等待旗──▶   V 举旗后
data_copy(z_gm[...], z_local[buf])   MTE3              旗到位后搬出
set_flag(MTE3_MTE2, buf)             MTE3              搬出完成后举旗
wait_flag(MTE3_MTE2, buf)            MTE2 ──等待旗──▶   下一轮覆写前的闸门
```

三个方向串成一条链：`MTE2 → V → MTE3 →（回到）MTE2`，把「搬入、计算、搬出」三段异步流水线接成有向无环的依赖图。任何一环缺 `wait_flag`，对应队列就可能读到/覆写尚未就绪的数据。

#### 4.2.3 源码精读

**枚举定义**——[python/asc/language/core/enums.py:86-121](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/enums.py#L86-L121) 定义 `HardEvent` 全部 35 个方向，`MTE2_V = 4`、`V_MTE3 = 7`、`MTE3_MTE2 = 19`。

**Python 接口**——[python/asc/language/basic/block_sync.py:35-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/block_sync.py#L35-L39) 是 `set_flag` 的实现：`event_id` 经 `materialize_ir_value` 物化成 IR 值（它可以是循环变量这样的运行时值），`event` 枚举则**原样**传给 `create_asc_SetFlagOp`——由 pybind 层转换成 IR 属性；[python/asc/language/basic/block_sync.py:47-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/block_sync.py#L47-L51) 的 `wait_flag` 结构完全对称。两者都挂 `@require_jit`，禁止在普通 Python 上下文调用。同文件还有 `pipe_barrier`（用 `PipeID`）、`cross_core_set_flag`（跨核同步）等兄弟接口，本讲不展开。

**IR Op 定义**——[include/ascir/Dialect/Asc/IR/Basic/OpBlockSync.td:47-56](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpBlockSync.td#L47-L56) 定义 `SetFlagOp`/`WaitFlagOp`：`event` 是 `AscendC_HardEventAttr`（编译期属性），`eventId` 是 `AnyType` 操作数（运行时值）。关键在 `SetFlagOp` 的 `paramTypeLists = [3, 0]`——对照 [lib/TableGen/include/Constant.h:44-50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/include/Constant.h#L44-L50)，`3` 是 `kInferEnumType`（枚举 → C++ 模板参数），`0` 是 `kNormalType`（普通实参）。也就是说，TableGen 据此自动生成 `SetFlag` 的发射代码：方向进模板、编号进括号。

**枚举属性定义**——[include/ascir/Dialect/Asc/IR/Core/Attributes.td:190-231](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L190-L231) 用 `I32EnumAttr` 定义 `HardEventAttr`，35 个成员的数值、小写字符串形式（`"mte2_v"` 等）与前端 `enums.py` 严格一致——前端枚举与 IR 属性是同一张表的两份镜像。

**发射到 Ascend C**——[lib/Target/AscendC/Basic/BlockSync.cpp:27-34](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Basic/BlockSync.cpp#L27-L34) 手写了 `WaitFlagOp` 的发射：输出形如 `AscendC::WaitFlag<AscendC::HardEvent::MTE2_V>(v)`——枚举变成 **C++ 模板参数**（编译进指令）、`eventId` 变成运行时实参。`SetFlagOp` 的发射函数则由 TableGen 按 `paramTypeLists` 自动生成，随 [lib/Target/AscendC/Translation.cpp:265-270](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L265-L270) 包含的 `AscendCOpEmit.cpp.inc` 并入发射重载集合，输出形如 `AscendC::SetFlag<AscendC::HardEvent::MTE2_V>(v)`。手写与自动生成两条路在最终产物上等价，这正是 u1-l3 所说「检索链」的一个实例。

**示例中的三对同步**——[examples/01_add/add.py:57-58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L57-L58)、[examples/01_add/add.py:63-64](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L63-L64)、[examples/01_add/add.py:68-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L68-L69) 分别是 `MTE2_V`、`V_MTE3`、`MTE3_MTE2` 三对 `set_flag`/`wait_flag`，第二参数都是 `buf_id`。官方开发指南对这套写法的定位见 [docs/pyasc_op_develop_guide.md:51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/pyasc_op_develop_guide.md#L51)：「并行流水任务之间使用 asc.set_flag/asc.wait_flag 接口完成同步」。

#### 4.2.4 代码实践

**实践目标**：通过「破坏一个同步」直观感受 `wait_flag` 的必要性。

**操作步骤**：

1. 复制 `examples/01_add/add.py` 为 `add_nosync.py`（保留原示例不动）。
2. 注释掉第 58 行 `asc.wait_flag(asc.HardEvent.MTE2_V, buf_id)`。
3. 在 Model 模式运行：`python3 add_nosync.py -r Model`。
4. 恢复第 58 行，改为注释第 64 行（`V_MTE3` 的 wait），再运行一次。
5. 两次都记录：是否报错、`torch.allclose` 断言是否通过、结果偏差多大。

**需要观察的现象**：V 流水线可能在 MTE2 尚未搬完时就读 `x_local`（步骤 3），或 MTE3 在 V 算完前就搬出旧值（步骤 4）。

**预期结果**：真机上应出现结果错误或不确定性偏差；但 Model 仿真器对队列竞争的模拟精度有限，可能表现为「结果仍然正确」或直接仿真报错——**待本地验证**。无论哪种现象，都请结合 4.2.2 的依赖链写下你的解释：缺了 `wait_flag` 后，哪条「执行条件」失去了保障。

#### 4.2.5 小练习与答案

**练习 1**：`HardEvent.V_MTE3` 中谁执行 `set_flag`、谁执行 `wait_flag`？

**答案**：按「生产者_消费者」规则，V 是生产者：`set_flag(V_MTE3)` 由 V 队列在计算完成后执行；`wait_flag(V_MTE3)` 在 MTE3 队列上等待，之后才允许搬出。

**练习 2**：事件方向（`HardEvent`）和事件编号（`event_id`）分别处在编译期还是运行期？

**答案**：方向是编译期属性：Python 枚举 → IR `HardEventAttr` → 发射为 C++ 模板参数 `HardEvent::MTE2_V`。编号是运行期值：可以是循环变量 `buf_id`（先经 `materialize_ir_value` 物化），发射为函数实参。

**练习 3**：如果把 `set_flag` 和 `wait_flag` 的书写顺序对调（先 wait 后 set），程序还正确吗？

**答案**：由于两条指令分属不同硬件队列、由各自队列按序执行，文本顺序的对调通常不影响硬件上的同步语义（每条指令只依赖本队列的前序指令和旗子状态）；但工程上应保持「先 set 后 wait」的惯例，与官方示例和文档一致，避免阅读混乱。此结论基于指令分发模型推导，具体行为「待本地验证」。

### 4.3 双缓冲流水：BUFFER_NUM=2 的重叠执行

#### 4.3.1 概念说明

单缓冲时，「搬入第 i 块 → 计算第 i 块 → 搬出第 i 块」只能串行：MTE2 搬的时候 V 在等，V 算的时候 MTE2 闲着。三段流水线任何时刻只有一段在工作。

双缓冲（`BUFFER_NUM=2`，乒乓缓冲）把 UB 上的缓冲分成两块：当 V 在计算第 i 块（`buf_id = i % 2 = 0`）时，MTE2 已经在往另一块（`buf_id = 1`）搬第 i+1 块。三段流水线从此重叠，理想情况下总耗时从三段之和压缩为「最慢一段 × 块数」。代价是 UB 占用翻倍——`BUFFER_NUM` 只能取 1 或 2 正是容量与并行度的折中。

#### 4.3.2 核心流程

先看缓冲怎么排（[examples/01_add/add.py:39-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L39-L47)）：

\[ \text{tile\_length} = \frac{\text{block\_length}}{\text{TILE\_NUM} \times \text{BUFFER\_NUM}}, \qquad \text{buffer\_size} = \text{tile\_length} \times \text{BUFFER\_NUM} \times \text{sizeof(dtype)} \]

每核 `block_length` 个元素被切成 `TILE_NUM × BUFFER_NUM` 个 tile；UB 上手工排布三块区域：`x_local` 从字节 0 起、`y_local` 从 `buffer_size` 起、`z_local` 从 \( 2 \times \text{buffer\_size} \) 起（示例第 47 行写作 `buffer_size + buffer_size`），每块容纳 `tile_length × BUFFER_NUM` 个元素。

再看循环体（i 从 0 到 `TILE_NUM × BUFFER_NUM - 1`）：

```text
i = 0 (buf 0):  MTE2 搬入 → buf0 ── V 算 buf0 ── MTE3 搬出 buf0
i = 1 (buf 1):  MTE2 搬入 → buf1   │  与 V 算 buf0 重叠 ── MTE3 搬出 buf1
i = 2 (buf 0):  MTE2 搬入 → buf0（覆写前必须等 i=0 全部完成）……
```

UB 侧切片用 `buf_id * tile_length`（[add.py:53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L53)），GM 侧切片用 `i * tile_length`（全局 tile 序号）——同一个 `data_copy` 里两个下标含义不同，这是读双缓冲代码的关键。

最后回答本讲的核心问题——**三对事件如何支撑起这套乒乓**，尤其是 `MTE3_MTE2` 为何存在：

- `MTE2_V` 保证：V 读 buf 前，本块已搬完。
- `V_MTE3` 保证：MTE3 搬出前，本块已算完。
- `MTE3_MTE2` 保证：MTE2 在第 i+2 轮**覆写同一 buf** 前，第 i 轮已完全搬出。

注意 `MTE3_MTE2` 表面上只约束「MTE3 → MTE2」，为什么足够防止 MTE2 覆写 V 还在读的数据？因为存在传递链：

\[ \text{MTE2 搬入}(i{+}2) \;>\; \text{MTE3 搬出}(i) \;>\; \text{V 计算}(i) \]

`MTE3 搬出(i)` 由 `V_MTE3` 挡在 `V 计算(i)` 之后，`MTE2 搬入(i+2)` 又由 `MTE3_MTE2` 挡在 `MTE3 搬出(i)` 之后，两级串联就把 MTE2 间接排在了 V 之后。这就是三个方向缺一不可、且必须**成对出现**的原因：每一对各守一条依赖边。

#### 4.3.3 源码精读

**缓冲数量约束**——[examples/01_add/add.py:21-23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L21-L23) 顶部的模块常量：`BUFFER_NUM = 2`，注释写明 "BUFFER_NUM should be 1 or 2"。`USE_CORE_NUM`、`TILE_NUM` 与它共同决定切分粒度。

**切分与手工排布**——[examples/01_add/add.py:39-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L39-L47)：第 39 行算 `tile_length`，第 42 行算 `buffer_size`（字节），第 45-47 行按 4.3.2 的公式排布三个 `LocalTensor`，每个长度都是 `tile_length * BUFFER_NUM`。手工模式必须自己保证三块区域不重叠（u2-l2 已强调）。

**乒乓循环**——[examples/01_add/add.py:49-54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L49-L54)：第 50 行 `buf_id = i % BUFFER_NUM` 在两块缓冲间交替；第 53-54 行两条 `data_copy` 把 GM 的第 i 个 tile 搬进 `buf_id` 号缓冲，UB 偏移 `buf_id * tile_length` 与 GM 偏移 `i * tile_length` 分别按各自坐标系计算。

**计算与搬出**——[examples/01_add/add.py:60-66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L60-L66)：`asc.add` 消费两块输入切片（受 `MTE2_V` 保护），`data_copy` 把结果切片搬回 GM（受 `V_MTE3` 保护）。

**闭环闸门**——[examples/01_add/add.py:68-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L68-L69)：`MTE3_MTE2` 的 set/wait 收尾，把 4.3.2 的传递依赖链闭合，允许下一轮安全覆写。

#### 4.3.4 代码实践

**实践目标**：对比 `BUFFER_NUM=1` 与 `2` 的正确性与耗时，验证「双缓冲换性能、不换正确性」。

**操作步骤**：

1. 复制 `examples/01_add/add.py` 为 `add_buf1.py`。
2. 把第 22 行改为 `BUFFER_NUM = 1`。
3. Model 模式运行，确认 `torch.allclose` 断言通过。
4. 用 `time python3 add_buf1.py -r Model`（或在 `vadd_launch` 前后加 `time.perf_counter()` 打点）记录耗时；对原版 `BUFFER_NUM=2` 做同样测量，各跑 3 次取均值。

**需要观察的现象**：

- 结果正确性：`tile_length = block_length // TILE_NUM // BUFFER_NUM` 与循环次数 `range(TILE_NUM * BUFFER_NUM)` 都随 `BUFFER_NUM` 自动适配，总搬运量不变。
- 缓冲排布：`buffer_size` 减半，`y_local`/`z_local` 的偏移地址随之前移——手工排布代码是参数化的，无需手改。
- 耗时：`BUFFER_NUM=1` 失去搬入/计算重叠，预期变慢；但 Model 仿真器的计时未必映射真机性能，趋势「待本地验证」，有条件时在 NPU 上复测。

**预期结果**：断言通过、数值与 `BUFFER_NUM=2` 完全一致；耗时差异记录实测值。若你在步骤中顺带 dump 了 `ascendc.cpp`，可以数一下 `SetFlag`/`WaitFlag` 调用次数的变化（循环次数减半，同步指令数也应减半），这是下一节综合实践的一部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么循环次数是 `TILE_NUM * BUFFER_NUM` 而不是 `TILE_NUM`？

**答案**：总 tile 数由切分粒度决定：每核 `block_length` ÷ `tile_length` = `TILE_NUM × BUFFER_NUM` 块。`BUFFER_NUM` 的语义不是「多切数据」而是「把连续两块 tile 拼进同一块更大的缓冲里乒乓使用」，所以循环上界随之翻倍、单块 `tile_length` 随之减半，总量不变。

**练习 2**：删掉第 68-69 行的 `MTE3_MTE2` 这一对，哪两个操作之间会产生竞争？

**答案**：第 i 轮的 `data_copy(z_gm[...], z_local[buf])`（MTE3 读）以及 V 对 `z_local[buf]` 的写入，与第 i+2 轮 MTE2 对同一 buf 区域的覆写之间失去顺序保障；由 4.3.2 的传递链，该事件同时保护 `x_local`/`y_local` 不被提前覆写（MTE2 间接排在 V 之后）。

**练习 3**：`event_id` 为什么传 `buf_id` 而不是固定 0？

**答案**：同一方向（如 `MTE2_V`）有编号互不干扰的多面旗。两块缓冲各自使用自己的事件编号，第 i 轮对 buf 0 的旗子不会与第 i+1 轮对 buf 1 的旗子混淆，乒乓才得以成立。若共用编号 0，两块缓冲的同步会互相阻塞甚至提前放行。

## 5. 综合实践

把本讲三个模块串成一份小报告——「Add 示例同步机制分析」：

1. **导出产物**：设置 `PYASC_DUMP_PATH`，分别运行 `BUFFER_NUM=2`（原版）与 `BUFFER_NUM=1`（4.3.4 的副本），得到两套 `codegen.mlir` 与 `ascendc.cpp`。
2. **IR 层观察**：在两份 `codegen.mlir` 中找到 `set_flag`/`wait_flag` 操作，确认 `event` 属性的打印形式（如 `mte2_v`）与 `event_id` 操作数；统计两者操作数量比（应为 2:1）。
3. **C 代码层观察**：在两份 `ascendc.cpp` 中找到 `AscendC::SetFlag<AscendC::HardEvent::MTE2_V>` 等 6 类调用（3 方向 × set/wait），对照 4.2.3 的发射源码理解「枚举 → 模板参数」的落地。
4. **破坏性实验**：按 4.2.4 分别注释三个 `wait_flag` 各跑一次，记录现象（报错信息或数值偏差）。
5. **产出**：写一页分析，包含：三方向依赖链图（可照 4.2.2 文字图改画）、`BUFFER_NUM` 1 vs 2 的耗时表、三个破坏实验的现象表，以及你对「Model 仿真器是否精确模拟队列竞争」的判断依据。

## 6. 本讲小结

- `TPosition` 是队列/缓冲区的**逻辑位置**（VECIN/VECOUT/VECCALC 矢量组、A1~CO2 矩阵组），与 `Hardware` 的**物理存储层次**（UB/L1/L0）构成「角色 → 落点」的映射；`LocalTensor` 支持的位置清单不含 GM。
- `HardEvent` 按「生产者_消费者」命名，`set_flag` 在源队列执行、`wait_flag` 在目的队列等待；方向是编译期属性（最终成为 C++ 模板参数），`event_id` 是运行时实参。
- Add 示例的同步链是 `MTE2 → V → MTE3 → MTE2` 闭环：`MTE2_V` 保护读前搬完、`V_MTE3` 保护搬出前算完、`MTE3_MTE2` 通过传递依赖防止跨轮覆写。
- `set_flag`/`wait_flag` 文本相邻但分属两条硬件队列，「相邻」不等于「串行」。
- 双缓冲 `BUFFER_NUM=2` 用翻倍 UB 换来搬入/计算重叠；改回 1 时切分参数自动适配、结果不变、失去重叠。
- 一条完整的传递路径值得记住：Python `IntEnum` → pybind → IR `I32EnumAttr`（小写字符串打印）→ TableGen `paramTypeLists` → Ascend C 模板参数。

## 7. 下一步学习建议

- **下一讲 u2-l5（基础 API）**：精读 `data_copy` 与 `asc.add` 的 Python 实现，本讲已预告的 `OverloadDispatcher`、`require_jit` 将在那里展开；你会看到 `data_copy` 正是 MTE2/MTE3 队列上指令的来源。
- **u2-l6（TPipe/TQue）**：把本讲的手动 `set_flag`/`wait_flag` 与 02_add_framework 的框架风格对照，理解 `alloc/enque/deque/free` 生命周期如何取代手动同步，并预习 `EraseSync`/`InsertSync` 两个 Pass（u6-l3 详讲自动同步插入，`VerifySync` 可校验你的同步合法性）。
- **延伸阅读源码**：[lib/Dialect/Asc/Transforms/InsertSync.cpp:67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L67) 一带可以看到编译器如何自动 `create<SetFlagOp>`——与本讲手写同步互为镜像；[include/ascir/Dialect/Asc/IR/Fwk/TPipe.td:71-93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TPipe.td#L71-L93) 则展示了 `HardEventAttr`/`TPositionAttr` 在队列 Op 中的更多用法。
