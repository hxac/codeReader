# C++ 单算子调用 Demo 实战

## 1. 本讲目标

本讲是「算子调用实战」的第一篇。前面几讲我们建立了 ATB 的整体认知（u1-l1）、目录结构（u1-l2）、构建方式（u1-l3）、核心数据类型（u1-l4）、Context（u1-l5）和 Operation 接口（u1-l6）。本讲把这些「零件」串起来，**亲手用 C++ 调通一个真实的 ATB 算子**。

学完后你应该能够：

- 说出 C++ 调用 ATB 算子的完整五段式骨架：**资源初始化 → 创建算子与装填 VariantPack → Setup → Execute → 资源释放**。
- 理解 `aclInit` / `aclrtSetDevice` / `CreateContext` / `CreateStream` 这套昇腾初始化套路与 ATB 的关系。
- 知道 `VariantPack` 如何装输入输出、`workspaceSize` 如何由 `Setup` 计算并由调用方分配。
- 学会用 `example/op_demo/faupdate/build.sh` 编译并运行一个单算子 demo，并理解其中的 ABI 自动探测逻辑。
- 具备举一反三的能力：换一个算子（如 `rms_norm`、`linear`）也能照着这套模板写出调用代码。

## 2. 前置知识

本讲假设你已经读过 u1-l4（Tensor / VariantPack）和 u1-l6（Operation 接口）。这里做最简回顾：

- **Tensor**：描述 + 数据分离。`tensor.desc` 描述 dtype/format/shape，`tensor.deviceData` 指向 NPU 显存。详见 [include/atb/types.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h)。
- **VariantPack**：算子的输入输出「集装箱」，只有 `inTensors` 和 `outTensors` 两个列表，顺序必须与算子定义一致。
- **Operation 两段式执行**：`Setup` 在 Host 侧做校验、形状推导、Tiling，算出 `workspaceSize`；`Execute` 才真正把任务异步下发到 Device，需要 `aclrtSynchronizeStream` 取结果。
- **Context**：一组算子共享的运行时环境，托管执行流、内存池等全局资源。

此外，本讲会用到昇腾计算语言（ACL，Ascend Computing Language）的基础接口。ACL 是 CANN 暴露给 Host（CPU）侧的 C 接口，负责设备管理、流管理、内存分配等「底层事务」；ATB 在其之上封装了高层算子。所以你会看到 demo 里 ACL 接口（`acl*`）与 ATB 接口（`atb::*`）混用：

| 层次 | 典型接口 | 作用 |
| --- | --- | --- |
| ACL（CANN 底层） | `aclInit`、`aclrtSetDevice`、`aclrtMalloc` | 初始化运行时、选卡、分配显存 |
| ATB（加速库） | `atb::CreateContext`、`CreateOperation`、`Setup`、`Execute` | 创建上下文、创建算子、执行算子 |

> 术语提示：**Host** 指 CPU 侧，**Device** 指 NPU（昇腾处理器）侧。`aclrtMemcpy(..., ACL_MEMCPY_HOST_TO_DEVICE)` 就是把 CPU 内存的数据拷到 NPU 显存。

## 3. 本讲源码地图

本讲围绕「faupdate 算子的 C++ 调用 demo」展开，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `example/op_demo/faupdate/faupdate_demo.cpp` | demo 主体，包含 `main` 函数，是本讲精读的核心。 |
| `example/op_demo/faupdate/build.sh` | 编译并运行该 demo 的脚本，演示 ABI 自动探测。 |
| `example/op_demo/demo_util.h` | demo 公共工具头：`CHECK_STATUS` 宏、`CreateTensor`、`CreateTensorFromVector` 等辅助函数。 |
| `example/op_demo/README.md` / `example/op_demo/faupdate/README.md` | demo 的使用说明与数据规格。 |
| `include/atb/infer_op_params.h` | `FaUpdateParam` 参数结构定义。 |
| `include/atb/operation.h` | `Operation` 抽象类的 `Setup`/`Execute`/`CreateOperation` 接口签名。 |

`example/op_demo` 目录下其实有 20+ 个算子 demo（`linear`、`rms_norm`、`self_attention`、`rope`、`paged_attention` 等），它们的结构完全一致：每个子目录一个 `<算子>_demo.cpp` + 一个 `build.sh`，统一 include 上层的 `demo_util.h`。**学会 faupdate 这一个，就学会了这一整套 demo 的读法。**

## 4. 核心概念与源码讲解

本讲覆盖两个最小模块：**4.1 demo 主流程（五段式骨架）** 与 **4.2 build.sh 编译脚本**。

### 4.1 C++ 调用算子的五段式主流程

#### 4.1.1 概念说明

无论调用哪个 ATB 算子，C++ 端的代码骨架都是固定的「五段式」。可以把一个算子的完整生命周期想象成「开店做生意」：

1. **资源初始化**：开门营业前先把电、水、柜台备好——初始化 ACL 运行时、选一张 NPU 卡、创建 Context 和执行流。
2. **创建算子 + 装填 VariantPack**：把要卖的货（输入 Tensor）摆上柜台，并准备好收货的筐（输出 Tensor），再把它们装进一个「订单箱」VariantPack。
3. **Setup（Host 侧准备）**：店员先核对订单、算好需要多大的临时堆放区（`workspaceSize`），但还没真正干活。
4. **Execute（Device 侧执行）**：店员按订单真正加工，加工是异步的，需要「等通知」（流同步）才能拿到成品。
5. **资源释放**：打烊，按「先创建的后释放」的相反顺序收拾：先放货/筐/workspace，再销毁算子、流、Context，最后关闭运行时。

这五段的顺序和「创建/销毁」的配对关系**必须严格对应**，否则会内存泄漏或崩溃。后面每个模块都会落回到这张地图。

> 关于本 demo 选用的算子 `faupdate`：它的作用是把 Flash Attention 分块计算产生的**局部中间结果**（`rowmax`、`rowsum`、`attention out`）合并成**全局结果**。在序列并行（SP）场景下，多张卡各算一段序列的 attention，需要这个算子把各段的局部统计量合并。其背后的数学原理是 Flash Attention 的「在线 softmax 归并」：设有两段的局部最大值 \(m_1, m_2\)、归一化系数 \(l_1, l_2\)、加权输出 \(o_1, o_2\)，则合并为全局值时

\[
m = \max(m_1, m_2), \quad l = e^{m_1-m} l_1 + e^{m_2-m} l_2, \quad o = \frac{e^{m_1-m} l_1 o_1 + e^{m_2-m} l_2 o_2}{l}
\]

本讲不深入这个公式，只需理解「faupdate 是一个做合并的融合算子」，是 ATB 把一段多步计算打包成单个算子的典型例子。（以上公式为 Flash Attention 通用归并原理，用于建立直觉，并非该算子 kernel 的逐行实现。）

#### 4.1.2 核心流程

demo 的 `main` 函数按下面这条主线推进（伪代码）：

```text
aclInit(nullptr)              # ① 初始化 ACL 运行时
aclrtSetDevice(0)             #    选择 0 号 NPU 卡
atb::CreateContext(&context)  #    创建 ATB 上下文
aclrtCreateStream(&stream)    #    创建执行流
context->SetExecuteStream(stream)

CreateOperation(param, &op)            # ② 创建算子（带参数 Param）
variantPack.inTensors  = {lse, localout}  #   装输入
variantPack.outTensors = {output}         #   装输出

op->Setup(variantPack, workspaceSize, context)  # ③ Host 侧准备，得到 workspaceSize
if workspaceSize > 0:
    aclrtMalloc(&workspacePtr, workspaceSize)    #   调用方分配 workspace

op->Execute(variantPack, workspacePtr, workspaceSize, context)  # ④ 下发到 Device
aclrtSynchronizeStream(stream)                  #   等待计算完成

aclrtFree(每个 in/out tensor)     # ⑤ 释放资源
aclrtFree(workspacePtr)
DestroyOperation(op)             #   算子先销毁
aclrtDestroyStream(stream)
DestroyContext(context)          #   context 后销毁
aclFinalize()
```

记忆口诀：**「初始化四件套 → 建算子装包裹 → Setup 量尺寸 → Execute 跑同步 → 释放反着来」**。

#### 4.1.3 源码精读

下面按五段式逐段精读 `faupdate_demo.cpp`。先看文件顶部的常量定义，它们决定了输入输出的形状与并行度：

[example/op_demo/faupdate/faupdate_demo.cpp:13-19](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L13-L19) 定义了卡号、lse/localout 各维大小、headSize、以及序列并行度 `SP_PARA_DEGREE=8`。后续构造 Tensor 时直接复用这些常量。

**① 资源初始化（四件套）**

[example/op_demo/faupdate/faupdate_demo.cpp:63-67](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L63-L67) 是整套初始化套路：`aclInit` 初始化 ACL 运行时（传 `nullptr` 表示用默认配置），`aclrtSetDevice(0)` 选定 0 号卡，`atb::CreateContext(&context)` 创建 ATB 上下文，`aclrtCreateStream(&stream)` 创建执行流，最后 `context->SetExecuteStream(stream)` 把流绑定到 context。之后该 context 下所有算子的 `Execute` 都会下发到这条流上。

注意这里 ACL 与 ATB 的分工：ACL 管「设备/流/内存」这种底层事务，ATB 管「上下文/算子」这种高层抽象。`CHECK_STATUS` 是 demo 自定义的宏，遇到非 0 错误码就打印并 `return`，定义在 [example/op_demo/demo_util.h:30-41](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/demo_util.h#L30-L41)，它会根据错误码范围区分是 ACL 错误还是 ATB 错误并给出对应文档链接。

**② 创建算子与装填 VariantPack**

先看算子是如何创建的。[example/op_demo/faupdate/faupdate_demo.cpp:48-55](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L48-L55) 构造一个 `atb::infer::FaUpdateParam` 参数对象，设置 `faUpdateType = DECODE_UPDATE` 和 `sp = 8`（序列并行度），再调用模板工厂 `atb::CreateOperation(param, faupdateOp)` 创建算子实例。

`FaUpdateParam` 的字段定义见 [include/atb/infer_op_params.h:3043-3068](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/infer_op_params.h#L3043-L3068)：除了 `faUpdateType`、`sp` 之外，还有一个 `uint8_t rsv[64]` 预留字段（用于版本兼容，详见 u1-l6 的 Param `rsv` 约定）。

接着装填输入输出。[example/op_demo/faupdate/faupdate_demo.cpp:73-78](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L73-L78) 把准备好的输入放进 `variantPack.inTensors`、输出放进 `variantPack.outTensors`。输入由辅助函数 `PrepareInTensor` 构造：

[example/op_demo/faupdate/faupdate_demo.cpp:28-41](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L28-L41) 用 `CreateTensorFromVector` 从一个 `std::vector<float>` 创建两个输入：`lse` 形状 `[8, 16384]`、`localout` 形状 `[8, 16384, 128]`。这个工具函数内部会做 H2D 拷贝（把 CPU 数据搬到 NPU），定义在 [example/op_demo/demo_util.h:207-243](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/demo_util.h#L207-L243)。

输出 tensor 不需要填初值，只需分配显存。[example/op_demo/faupdate/faupdate_demo.cpp:76-77](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L76-L77) 调用 `CreateTensor` 创建形状为 `{LOCALOUT_DIM_1, HEAD_SIZE}` 即 `[16384, 128]` 的输出。`CreateTensor` 会填充 `desc` 并 `aclrtMalloc` 分配显存，见 [example/op_demo/demo_util.h:64-77](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/demo_util.h#L64-L77)。

> 数据规格对照（来自 [example/op_demo/faupdate/README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/README.md)）：`lse` = float/nd/`[8,16384]`，`localout` = float/nd/`[8,16384,128]`，`output` = float/nd/`[16384,128]`。注意 demo 里输入只填了 `{1,2,3,4,5,6}` 这几个样例值，并不代表真实场景数据，README 也明确说明「示例中生成的数据不代表实际场景」。

**③ Setup：Host 侧准备，量出 workspaceSize**

[example/op_demo/faupdate/faupdate_demo.cpp:80-86](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L80-L86) 是两段式执行的第一段：调用 `faupdateOp->Setup(variantPack, workspaceSize, context)`，`Setup` 在 Host 侧完成校验、形状推导、Tiling 切分，并**输出**算子需要的临时工作区大小 `workspaceSize`。随后**调用方**（不是 ATB）负责按这个大小 `aclrtMalloc` 分配 workspace 显存。这正是 u1-l6 强调的「`Setup` 不碰真实数据、只算尺寸」。

`Setup` 的接口签名见 [include/atb/operation.h:83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L83)：第二个参数 `uint64_t &workspaceSize` 是引用传出。若 `workspaceSize == 0` 则该算子不需要 workspace，跳过分配。

**④ Execute：下发到 Device 并同步**

[example/op_demo/faupdate/faupdate_demo.cpp:88-89](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L88-L89) 是两段式执行的第二段：`Execute` 携带 `workspacePtr` 和 `workspaceSize` 把任务**异步**下发到 Device。因为是异步的，紧接着必须 `aclrtSynchronizeStream(stream)` 阻塞等待这条流上的所有任务完成，此时输出 tensor 的显存里才真正有结果。

`Execute` 的签名见 [include/atb/operation.h:97-98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L97-L98)，第二参数是 `uint8_t *workspace`（没有 workspace 时传 `nullptr` 即可）。

**⑤ 资源释放（反着来）**

[example/op_demo/faupdate/faupdate_demo.cpp:92-104](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/faupdate_demo.cpp#L92-L104) 按与创建相反的顺序释放。关键顺序要点（与 u1-l6 的生命周期约定一致）：

1. 先 `aclrtFree` 每个 in/out tensor 的 `deviceData`、再 free `workspacePtr`（这些是数据/缓冲，最先释放）。
2. 然后 `atb::DestroyOperation(faupdateOp)`——**算子对象先于 Context 释放**（注释也写明「operation，对象概念，先释放」）。
3. 接着 `aclrtDestroyStream`。
4. 再 `DestroyContext(context)`——context 作为全局资源后释放。
5. 最后 `aclFinalize()` 关闭 ACL 运行时。

`DestroyOperation` 与 `CreateOperation` 配对，定义见 [include/atb/operation.h:120](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/operation.h#L120)，其注释明确「执行完后需要调用 DestroyOperation 销毁，否则将导致内存泄漏」。若一切顺利，最后一行打印 `faupdate demo success!`。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读」的方式，把五段式骨架迁移到另一个算子 demo 上，验证「骨架通用、只有 Param/Tensor 不同」这一结论。

**操作步骤**：

1. 打开 [example/op_demo/rms_norm/rms_norm_operation... 目录下的 demo cpp 文件](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/)（也可选 `linear` 或 `layer_norm`）。先用 `Glob`/`ls` 看该子目录里的 cpp 文件名。
2. 对照本讲的五段式，在该 demo 里分别找到并标注：
   - **资源初始化**：`aclInit` / `aclrtSetDevice` / `CreateContext` / `CreateStream` / `SetExecuteStream` 这几行。
   - **创建算子**：它用的是哪个 `Param` 结构？字段有何不同？
   - **VariantPack 装填**：它的输入输出 tensor 各是几个、什么形状？
   - **Setup → Execute → Sync**：与 faupdate 是否一字不差地一致？
   - **资源释放**：销毁顺序是否同样是「算子先、context 后」。
3. 列一张对照表：faupdate demo 与你选的 demo，在「Param 类型、输入个数、输出个数、是否需要 workspace」上的异同。

**需要观察的现象**：

- 五段式的「初始化」「Setup→Execute→Sync」「释放」三段在两个 demo 里几乎是**复制粘贴**的；只有「创建算子的 Param」和「Tensor 形状/个数」不同。
- 这说明 ATB 把调用套路做成了高度统一的模板，掌握一个就掌握全部。

**预期结果**：你能写出一张如下的对照表（以 faupdate 为例，第二行由你填写）：

| 算子 | Param 类型 | 输入个数 | 输出个数 | 备注 |
| --- | --- | --- | --- | --- |
| faupdate | `FaUpdateParam` | 2 (lse, localout) | 1 (output) | sp=8 |
| rms_norm | （你来填） | （你来填） | （你来填） | （你来填） |

> 待本地验证：若你手头有 Atlas A2/A3 环境，可按 4.2 的方法实际编译运行对比；否则本实践以源码阅读 + 填表为准。注意 faupdate 仅支持 Atlas A2/A3 系列产品（见其 README「产品支持情况」）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `aclrtSynchronizeStream(stream)` 这一行删掉，程序会出现什么问题？为什么？

**参考答案**：`Execute` 是**异步下发**，它返回时 Device 上的计算大概率还没完成。删掉同步会导致：紧接着的 `aclrtFree(outTensor.deviceData)` 可能在算子还在写这块显存时就执行，造成结果错误甚至段错误；并且你在 Host 侧读到的输出 tensor 是未定义的旧数据。同步这一步是「等 Device 算完」的必要手段。

**练习 2**：`Setup` 阶段分配的 workspace 是干什么用的？为什么由调用方（demo）而不是 ATB 内部分配？

**参考答案**：workspace 是算子在 Device 上执行时需要的**临时工作显存**（例如中间结果、tiling 数据等），大小因输入形状和参数而异。由调用方分配有两个好处：一是把内存分配权交给上层，便于复用同一块显存、统一管理显存池（在大模型推理中可显著省显存）；二是 `Setup` 只算尺寸不分配，使得「形状推导」与「资源分配」解耦，符合「描述与数据分离」的设计。这正是 ATB 两段式 `Setup → Execute` 的核心动机。

**练习 3**：把资源释放顺序里的 `DestroyOperation(faupdateOp)` 和 `DestroyContext(context)` 对调会怎样？

**参考答案**：会出问题。算子对象在创建时可能与 context 关联（例如注册到 context 的 RunnerPool、使用 context 的资源池）。若先销毁 context，算子销毁时访问的 context 资源已成野指针，可能崩溃或泄漏。正确顺序是**算子先于 context 释放**，即注释里强调的「operation，对象概念，先释放；context，全局资源，后释放」。

### 4.2 build.sh 编译脚本

#### 4.2.1 概念说明

写完 cpp 还要能编译运行。demo 的 `build.sh` 是一个极简的编译脚本，但它示范了 ATB 对接上层代码时**最容易踩坑的一件事：CXX11 ABI 对齐**。

回顾 u1-l3：ATB 在编译时用 `USE_CXX11_ABI` 开关产出两套互不兼容的库（`cxx_abi_0` / `cxx_abi_1`）。如果你的应用（这里就是 demo）和 ATB 库用了不同的 ABI，链接期或运行期就会出错。所以 demo 编译时**必须让 g++ 的 ABI 宏与已安装的 ATB 库保持一致**。`build.sh` 的核心就是「先探测正确的 ABI，再带着对应的 `-D_GLIBCXX_USE_CXX11_ABI` 去编译」。

#### 4.2.2 核心流程

`build.sh` 做三件事：

```text
1. 用 python3 探测 PyTorch 的 ABI（torch.compiled_with_cxx11_abi()）
   -> 得到 cxx_abi = 0 或 1（探测失败时默认 1）
2. g++ 带上 -D_GLIBCXX_USE_CXX11_ABI=$cxx_abi，
   并 -I 指向 ATB 和 CANN 的 include、-L 指向两者的 lib，
   链接 -l atb -l ascendcl 编译出可执行文件
3. 直接 ./faupdate_demo 运行
```

#### 4.2.3 源码精读

[example/op_demo/faupdate/build.sh:12-20](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/build.sh#L12-L20) 是 ABI 探测：调用 `python3 -c '... torch.compiled_with_cxx11_abi() ...'`，返回 `"1"` 或 `"0"`；若环境没装 torch（`ImportError`）则默认 `"1"`。这复用了 ATB 主构建 `scripts/build.sh` 同样的「以 PyTorch ABI 为准」的对齐思路（详见 u1-l3）。

[example/op_demo/faupdate/build.sh:22-24](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/build.sh#L22-L24) 是真正的编译与运行命令，关键参数解读：

| 参数 | 含义 |
| --- | --- |
| `-D_GLIBCXX_USE_CXX11_ABI=$cxx_abi` | 让 demo 与 ATB 库的 C++ 标准库 ABI 对齐（最关键）。 |
| `-I "${ATB_HOME_PATH}/include"` | ATB 头文件目录（`atb_infer.h` 等在这里）。 |
| `-I "${ASCEND_HOME_PATH}/include"` | CANN/ACL 头文件目录（`acl/acl.h`）。 |
| `-L "${ATB_HOME_PATH}/lib"` | ATB 库目录（`libatb.so`）。 |
| `-L "${ASCEND_HOME_PATH}/lib64"` | CANN 库目录（`libascendcl.so`）。 |
| `faupdate_demo.cpp ../demo_util.h` | 编译 demo 主体，并包含公共工具头。 |
| `-l atb -l ascendcl` | 链接 ATB 和 ACL 两个库。 |
| `-o faupdate_demo` | 输出可执行文件。 |

这里出现的两个环境变量 `ATB_HOME_PATH` 和 `ASCEND_HOME_PATH` 正是 u1-l3 讲过的：前者指向 ATB 安装根目录（`source .../output/atb/set_env.sh` 后可用），后者指向 CANN 安装根目录。所以运行 demo 前必须先 `source` 好 CANN 和 ATB 的 `set_env.sh`，具体见 [example/op_demo/faupdate/README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/op_demo/faupdate/README.md) 的「使用说明」一节。

> 小贴士：`build.sh` 末尾直接 `./faupdate_demo` 运行。该 demo 仅支持 Atlas A2/A3 系列产品；在没有 NPU 的机器上编译能过，但运行会因 `aclrtSetDevice` 找不到设备而失败。

#### 4.2.4 代码实践

**实践目标**：把 `build.sh` 改造成可以编译运行 `example/op_demo` 下**任意**一个算子 demo，体会「骨架一致、只换源文件名」。

**操作步骤**：

1. 复制 `example/op_demo/faupdate/build.sh` 到一个临时脚本（不要改原文件）。
2. 找一个目标算子目录，例如 `example/op_demo/rms_norm/`，记下其中的 cpp 文件名（如 `rms_norm_demo.cpp`）。
3. 把临时脚本最后一行的源文件名 `faupdate_demo.cpp` 改成目标文件名、把 `../demo_util.h` 路径保持不变（因为所有 demo 都共用上层目录的这个头）、把 `-o` 后的输出名也改成对应名字。
4. 先 `source` CANN 与 ATB 的 `set_env.sh`，再 `bash` 你的临时脚本。

**需要观察的现象**：

- 只要 `ATB_HOME_PATH` / `ASCEND_HOME_PATH` 正确、ABI 探测成功，不同 demo 的编译命令**完全一样**，只是源文件名不同。
- 若忘记 `source set_env.sh`，会报找不到 `atb/atb_infer.h` 或链接时找不到 `-l atb`。

**预期结果**：成功时对应 demo 打印 `<算子> demo success!`。

> 待本地验证：本实践需要真实的昇腾环境（Atlas A2/A3）。在无 NPU 的环境里，你只能完成「改脚本 + 解释每个参数含义」的部分，实际编译运行需到具备硬件的机器上验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `build.sh` 要用 `torch.compiled_with_cxx11_abi()` 来决定 ABI，而不是直接写死 `1`？

**参考答案**：因为 demo 要链接的是**已安装的 ATB 库**，而 ATB 库在主构建时本身就是按 PyTorch 的 ABI 来对齐产出的（见 u1-l3）。如果用户换了不同 ABI 的 PyTorch 重装了 ATB，对应的 ATB 库 ABI 也会变。以 torch 的 ABI 为准，能保证 demo 始终和「这套环境里实际装好的 ATB」对齐，避免链接/运行期 ABI 不匹配错误。探测失败时默认取 `1`（CXX11 ABI），这是较新工具链的常见取值。

**练习 2**：`-I` 和 `-L` 分别解决什么问题？少了 `-l atb` 会怎样？

**参考答案**：`-I` 指定**头文件搜索路径**，让 `#include "atb/atb_infer.h"` 和 `#include "acl/acl.h"` 能被找到（编译期）；`-L` 指定**库文件搜索路径**，让链接器知道去哪里找 `.so`（链接期）。`-l atb` 则告诉链接器**真正去链接 `libatb.so`**——有 `-L` 只是「告诉它去哪找」，还得有 `-l` 才会「真的链接」。少了 `-l atb` 会在链接期报 `undefined reference to atb::CreateOperation` 之类的符号未定义错误。

**练习 3**：README 提示「如需编译其他 demo，需要替换 `faupdate_demo` 为对应的 cpp 文件名」。这句话背后反映了 demo 的什么设计约定？

**参考答案**：反映了「**所有 op_demo 共用同一套编译模板与同一个 `demo_util.h`，彼此结构高度同构**」的约定。每个 demo 的差异仅在算子自身的 cpp 文件（Param 与 Tensor 不同），编译方式、依赖的头/库、ABI 处理完全一致。因此切换 demo 只需替换源文件名，无需改编译逻辑。这也使得本讲学到的「五段式骨架 + build.sh 编译」可以无缝迁移到任意算子。

## 5. 综合实践

**任务**：选择 `example/op_demo` 下的 `linear` 或 `layer_norm` 算子，**手工写出调用它的 C++ 主流程伪代码**（不必真正编译），要求覆盖完整的五段式，并写出对应的编译命令。

具体要求：

1. 先阅读该算子的 demo cpp 与其 `Param` 定义（在 `include/atb/infer_op_params.h` 中找到，例如 `LinearParam` / `LayerNormParam`），记录它的：输入输出个数、关键 Param 字段、是否需要 workspace。
2. 仿照本讲 4.1.2 的伪代码格式，写出该算子的五段式主流程，重点写清楚：用了哪个 Param、VariantPack 里装了哪些 tensor（写出形状）、`Setup`/`Execute`/`aclrtSynchronizeStream` 三步、以及完整的释放顺序。
3. 仿照本讲 4.2.3，写出编译该 demo 的 `g++` 命令（含 ABI 探测思路、`-I`/`-L`/`-l` 参数）。
4. 最后用一段话说明：相比 faupdate，这个算子在调用流程上有哪些相同点、哪些不同点。

**评判标准**：相同点应能指出「初始化四件套、Setup→Execute→Sync、释放反着来」完全一致；不同点应集中在 Param 类型与 tensor 形状/个数上。能准确说出这两点，就说明你真正掌握了 ATB 单算子 C++ 调用的通用模板。

> 这个综合实践把「读源码 → 理解 Param/Tensor → 复用骨架 → 理解编译」串了起来，是后续学习 Python 调用（u2-l2）和参数体系（u2-l3）的实弹演习。

## 6. 本讲小结

- C++ 调用任意 ATB 算子都遵循固定的**五段式骨架**：资源初始化（ACL+ATB 四件套）→ 创建算子并装填 VariantPack → Setup → Execute+同步 → 资源释放。
- 初始化是「ACL 管底层（设备/流/内存）、ATB 管高层（Context/算子）」的混用，`aclInit`/`aclrtSetDevice`/`CreateContext`/`CreateStream`/`SetExecuteStream` 缺一不可。
- 算子由模板工厂 `CreateOperation<Param>` 创建，参数装在 `VariantPack` 的 `inTensors`/`outTensors` 里，**顺序必须与算子定义一致**。
- 两段式 `Setup → Execute` 的本质是「描述与数据分离」：`Setup` 只在 Host 算尺寸（含 `workspaceSize`），`Execute` 才异步下发到 Device，必须 `aclrtSynchronizeStream` 才能拿到结果。
- 资源释放**反着来**：先放 tensor/workspace，再 `DestroyOperation`（算子先），再销毁流、`DestroyContext`（context 后），最后 `aclFinalize`。
- `build.sh` 的核心是 **CXX11 ABI 自动对齐**（以 PyTorch 为准），再用 `-I/-L/-l` 对接 ATB 与 CANN 的头文件和库；所有 op_demo 共用同一编译模板。

## 7. 下一步学习建议

- **下一篇 u2-l2（Python/torch_atb 调用算子）**：本讲用 C++ 跑通了算子，下一篇会用 Python 以更简洁的方式做同样的事，对比两者能加深对 ATB 接口设计的理解。
- **u2-l3（算子参数体系与公共枚举）**：本讲只用到 `FaUpdateParam`，下一篇系统讲解 `infer_op_params.h` 里的 Param 命名约定、`rsv` 预留字段与公共枚举，帮你快速读懂任意算子的参数。
- **想深入执行链路**：可预习单元 3，尤其是 u3-l1（OperationBase）和 u3-l2（Runner），理解 `Setup`/`Execute` 内部到底做了什么。
- **想实际跑起来**：到具备 Atlas A2/A3 硬件的机器上，`source` 好两份 `set_env.sh`，按本讲 4.2 的步骤 `bash build.sh`，亲手看到 `faupdate demo success!` 输出。
