# 端到端快速上手：样例运行与体验

## 1. 本讲目标

本讲是入门单元的最后一讲，目标是让你**先把一条完整的链路跑通、看到整体效果**，再在后续单元里逐层深入源码。

学完后你应当能够：

- 知道 GE 仓库里 `examples/` 样例目录是怎么组织的，能快速找到对应场景的样例。
- 复述「下载 ONNX 模型 → 用 `atc` 编译成 OM → 用 ACL 接口加载并执行 OM」这条离线端到端流程。
- 对照 ResNet50 样例的 C++ 源码，说出 ACL 加载模型、执行推理、取回结果的关键步骤与对应接口。
- 识别 LLM（大语言模型）样例与普通 CV 样例在 `atc` 编译参数上的差异，能定位并阅读一个 LLM 样例。

> 提醒：本讲只把 `atc` 当作一个**命令行黑盒**来使用，不深入它的内部实现（那是后面单元 3 的内容）；本讲的重点是「用样例串起 GE 的离线使用全流程」。

## 2. 前置知识

在进入样例之前，先回顾前几讲已经建立的关键认知（本讲会直接用到，不再重复解释）：

- **GE 的离线场景**（u1-l1）：用 `atc` 把模型文件编译成 OM 离线模型产物，编译阶段**不需要昇腾设备**，也**不需要前端框架运行时**；OM 可以独立部署到设备上执行。
- **OM 是 GE Compiler 的产物**（u1-l1）：`atc` 调用 GE 的编译器，做完图优化、算子编译、流分配、内存规划后，把结果序列化成 OM 文件。
- **Host / Device 的概念**（u1-l1）：Host 是宿主机（CPU 侧），Device 是昇腾芯片侧；推理时输入数据要搬到 Device，结果也要从 Device 取回。
- **顶层目录导航**（u1-l2）：样例代码放在仓库的 `examples/` 下，它们是「使用 GE 产物」的示例，本身不是 GE 编译器的核心源码。

本讲还会出现两个新名词，先做通俗解释：

- **ACL（Ascend Computing Language）**：面向昇腾的 C 语言编程接口，是应用程序调用 GE 能力的「用户侧入口」。样例里那些以 `acl` 开头的函数（如 `aclInit`、`aclmdlExecute`）就是 ACL 接口；应用通过 ACL 加载 OM 并下发执行，底层由 GE 的执行器（Executor）把算子真正跑在硅片上。
- **OM（Offline Model）**：GE 编译后产出的离线模型文件，里面已经包含了设备相关的算子二进制、内存布局、任务序列等信息，可以被 ACL 直接加载执行。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `examples/README.md` | 样例总入口，列出全部场景样例的导航表。 |
| `examples/acl/README.md` | ACL 运行时样例子目录的导航表。 |
| `examples/acl/1_sample_resnet50_imagenet_classification/README.md` | ResNet50 单 Batch 图片分类样例说明（含 `atc` 命令、目录结构、预期输出）。 |
| `examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp` | ResNet50 样例主程序，演示 ACL 完整生命周期。 |
| `examples/acl/common/sampleDevice.h` | ACL 设备/上下文/流的 RAII 封装（`aclInit`、`aclrtSetDevice` 等）。 |
| `examples/acl/common/sampleModel.h` | ACL 模型加载/描述的 RAII 封装（`aclmdlLoadFromFileWithMem`、`aclmdlGetDesc` 等）。 |
| `examples/acl/2_sample_resnet50_imagenet_classification_dynamic_batch/README.md` | ResNet50 动态 Batch 样例说明，用于对比「固定 Batch」与「动态 Batch」。 |
| `examples/acl/3_sample_qwen_llm/README.md` | Qwen LLM 推理样例说明，演示大模型的加载与推理。 |

---

## 4. 核心概念与源码讲解

### 4.1 样例目录概览

#### 4.1.1 概念说明

GE 仓库的 `examples/` 目录收录了不同场景的**调用样例**，目的是让你「搭好环境就能照着跑」。这些样例不是 GE 编译器/执行器本身的源码，而是**站在使用者角度**演示「怎么把 GE 产物用起来」的最小可运行示例。

样例大致分两类：

- **ACL 运行时样例**（`examples/acl/`）：演示「拿到 OM 后，怎么用 ACL 接口加载并执行」。这是最贴近真实部署场景的一类，也是本讲的主角。
- **特性样例**（其余子目录）：演示 GE 的某项具体能力，例如 `fusion_pass/`（自定义融合规则）、`custom_op/`（自定义算子入图）、`es/`（ES 构图）、`dflow/`（异步流水）、`offline_compile_run/`（离线图编译执行）等。这些会在后续专家单元逐一展开。

#### 4.1.2 核心流程

不管哪个样例，离线场景的骨架都是同一条链路：

```text
准备模型文件(如 .onnx)
        │
        ▼
   atc 编译  ──►  产出 OM（GE Compiler 的工作，编译期不需设备）
        │
        ▼
   准备输入数据(预处理成模型需要的格式)
        │
        ▼
   编译样例程序(bash scripts/build.sh)
        │
        ▼
   运行样例(bash scripts/run.sh)
        │
        ▼  程序内部：ACL 初始化 → 加载 OM → 执行 → 取回结果
   得到推理输出
```

注意这条链路正好印证了 u1-l1 讲过的「**编译与执行分离**」：前两步（下模型、`atc` 编译）在普通主机上就能完成，后几步（加载、执行）才需要昇腾设备。

#### 4.1.3 源码精读

样例总入口是一张导航表，列出了全部场景样例：

[examples/README.md:3-15](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/README.md#L3-L15) —— 这是样例目录的「目录页」，每个场景对应一行链接。

其中 ACL 运行时样例集中在 `examples/acl/` 下，有自己的子导航表：

[examples/acl/README.md:3-11](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/README.md#L3-L11) —— 列出了 ResNet50、ResNet50 动态 Batch、Qwen LLM、ACL GEMM、YOLOv13 等样例。

由此可见，样例目录的组织逻辑是「**按场景分目录、每个目录一个独立可运行样例**」。后两小节我们就挑其中最有代表性的两个——ResNet50（CV 分类）和 Qwen（LLM）——来拆解。

#### 4.1.4 代码实践

**实践目标**：熟悉样例目录的导航方式，建立全局地图。

**操作步骤**：

1. 打开 `examples/README.md`，浏览整张样例导航表。
2. 打开 `examples/acl/README.md`，数一数 ACL 子目录下共有几个样例。
3. 用下表把每个 ACL 样例与它的「场景」对应起来（下表为参考答案，可先自己填再对照）：

| 样例编号 | 目录 | 场景 |
|---------|------|------|
| 1 | `1_sample_resnet50_imagenet_classification` | ResNet50 固定单 Batch 图片分类 |
| 2 | `2_sample_resnet50_imagenet_classification_dynamic_batch` | ResNet50 动态 Batch 图片分类 |
| 3 | `3_sample_qwen_llm` | Qwen 大语言模型推理 |
| 4 | `4_sample_acl_gemm` | ACL 矩阵乘（GEMM） |
| 5 | `5_sample_yolov13` | YOLOv13 目标检测 |

**需要观察的现象**：ACL 样例统一用「数字前缀 + 场景描述」命名，编号基本反映了由浅入深的顺序（先固定 Batch，再动态 Batch，再到 LLM）。

**预期结果**：你能在 30 秒内根据需求（例如「我想跑一个大模型样例」）定位到正确目录。

#### 4.1.5 小练习与答案

**练习 1**：如果我只想验证 GE 的融合规则怎么写，应该看哪个样例目录？
> **答案**：`examples/fusion_pass/`，它是专门演示自定义融合 Pass 的特性样例。

**练习 2**：ACL 样例和 `offline_compile_run` 样例的侧重点有什么不同？
> **答案**：ACL 样例侧重「拿到 OM 之后用 ACL 接口加载执行」（部署视角）；`offline_compile_run` 侧重「用 GE API 在程序内离线构图、编译再执行」（开发视角）。本讲聚焦前者。

---

### 4.2 ResNet50 推理样例流程

#### 4.2.1 概念说明

`1_sample_resnet50_imagenet_classification` 是最经典的入门样例：基于 ONNX 的 ResNet-50 网络，对两张狗的图片做分类，输出 Top-5 置信度的类别标识。它麻雀虽小五脏俱全，完整覆盖了「编译 → 加载 → 执行 → 取结果」四个阶段，是理解 GE 离线用法最好的切入点。

这个样例之所以适合入门，是因为它的输入非常简单：**单输入、固定单 Batch**——一张图片对应一次推理，不需要处理动态形状，`atc` 编译时连 `--input_shape` 都不用写（shape 由 ONNX 直接给出）。

#### 4.2.2 核心流程

整个样例的运行流程分为「准备」和「程序内部执行」两层。

**准备阶段（命令行）**：

1. 下载 ONNX 模型与测试图片。
2. 用 `atc` 把 ONNX 编译成 OM。
3. 用 `transfer_pic.py` 把 jpg 图片预处理成模型需要的 bin 格式（缩放、中心裁剪、归一化、转 NCHW）。
4. `bash scripts/build.sh` 编译样例 C++ 程序；`bash scripts/run.sh` 运行。

**程序内部执行（ACL 生命周期）**，是一个有严格先后顺序的状态序列：

```text
aclInit            # 1. 初始化 ACL 运行环境（进程级，全局一次）
   │
   ▼
aclrtSetDevice     # 2. 指定用哪张昇腾卡（Device 0）
aclrtCreateContext # 3. 创建上下文（Context，持有设备资源）
aclrtCreateStream  # 4. 创建流（Stream，执行任务的队列）
   │
   ▼
aclmdlQuerySize            # 5. 查询加载该 OM 需要多大的工作/权重内存
aclrtMalloc (work/weight)  # 6. 在 Device 上预分配这两块内存
aclmdlLoadFromFileWithMem  # 7. 把 OM 加载进预分配的内存，得到 modelId
aclmdlGetDesc              # 8. 获取模型描述（输入输出个数、shape、大小）
   │
   ▼ （以下对每张图片循环）
aclrtMalloc (input)                 # 9. 分配输入 Device 内存
MemcpyFileToDeviceBuffer            # 10. 把图片 bin 搬到 Device
aclmdlExecute(modelId, in, out)     # 11. 同步执行一次推理（核心）
aclrtMemcpy (device→host)           # 12. 把输出从 Device 拷回 Host 并解析 Top-5
```

这 12 步几乎覆盖了 ACL 模型推理的完整套路，记住这个骨架，后面看任何 ACL 样例都能对号入座。

#### 4.2.3 源码精读

**① `atc` 编译命令**。ResNet50 样例 README 给出的核心编译命令如下（在 `model/` 目录下执行）：

[examples/acl/1_sample_resnet50_imagenet_classification/README.md:71-74](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/README.md#L71-L74) —— 把 `resnet50_Opset16.onnx` 编译成 `resnet50.om`，指定框架为 ONNX、目标芯片型号、输入格式与输出类型。

其中关键参数含义：

[examples/acl/1_sample_resnet50_imagenet_classification/README.md:77-81](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/README.md#L77-L81) —— 参数表：`--framework=5` 表示 ONNX（0=Caffe、1=MindSpore、3=TensorFlow、5=ONNX）；`--soc_version` 指定昇腾处理器型号；`--output_type=FP32` 指定输出数据类型。

> 这条命令就是 u1-l1 所说「离线编译入口 `atc`」的实际用法。它产出 OM，编译过程在普通主机上完成，不需要昇腾卡。

**② 样例目录结构**。README 给出了样例的标准布局：

[examples/acl/1_sample_resnet50_imagenet_classification/README.md:30-48](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/README.md#L30-L48) —— `scripts/` 放编译运行脚本，`src/` 放主程序与 `acl.json` 配置，`model/` 与 `data/` 需手动创建，分别放 OM 和测试数据。

**③ ACL 初始化（步骤 1-4）**。主程序的 `InitResource` 完成环境与设备资源准备：

[examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp:45-72](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp#L45-L72) —— 依次构造 `AclInstance`（`aclInit`）、`AclDevice`（`aclrtSetDevice`）、`AclContext`（`aclrtCreateContext`）、`AclStream`（`aclrtCreateStream`），并在末尾用 `aclrtGetRunMode` 判断程序跑在 Host 还是 Device。

这里的 `AclInstance` 等是对裸 ACL 接口的 RAII 封装，构造时调用初始化、析构时调用清理。以 `AclInstance` 为例：

[examples/acl/common/sampleDevice.h:17-25](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/common/sampleDevice.h#L17-L25) —— 构造里 `aclInit`，析构里 `aclFinalize`，保证进程退出时正确收尾。同理 `AclDevice` 的 `aclrtSetDevice` / `aclrtResetDevice` 见同文件 [sampleDevice.h:54-66](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/common/sampleDevice.h#L54-L66)。

`aclrtGetRunMode` 那一步很关键：它把 u1-l1 讲的 Host/Device 概念落到了代码里——程序据此决定后面取输出时**要不要把数据从 Device 拷回 Host**。

**④ 加载模型（步骤 5-8）**。`PrepareModel` 负责把 OM 真正加载到设备：

[examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp:74-95](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp#L74-L95) —— 先 `aclmdlQuerySize` 查询加载该 OM 所需的工作内存与权重内存大小，用 `aclrtMalloc` 各自分配（封装在 `AclModelWork` / `AclModelWeight` 里），再 `aclmdlLoadFromFileWithMem` 把 OM 加载进这两块内存并拿到 `modelId`，最后 `AclModelDesc` 内部调用 `aclmdlGetDesc` 取得模型描述。

模型描述的获取与卸载同样做了 RAII 封装：

[examples/acl/common/sampleModel.h:56-74](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/common/sampleModel.h#L56-L74) —— 构造里 `aclmdlCreateDesc` + `aclmdlGetDesc`，析构里 `aclmdlUnload` + `aclmdlDestroyDesc`。

> 注意 `aclmdlLoadFromFileWithMem` 加载的就是 ① 里 `atc` 产出的 OM。至此，u1-l1 讲的「GE Compiler 产出 OM → GE Executor 加载执行」在代码里完成了闭环：编译产物（OM）被执行侧（ACL/Executor）加载。

**⑤ 执行与取结果（步骤 9-12）**。`Process` 对每张图片做一次推理：

[examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp:154-191](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp#L154-L191) —— 循环里先用 `Utils::MemcpyFileToDeviceBuffer` 把图片 bin 搬到 Device 输入缓冲，再用 `aclmdlExecute` 同步执行一次推理，最后 `OutputModelResult()` 把输出拷回 Host 并打印 Top-5。

核心的一行是同步执行：

[examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp:179](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp#L179) —— `aclmdlExecute(modelId, 输入dataset, 输出dataset)`，一次调用触发整模型在设备上执行（这就是 u1-l6/单元 6 将讲的「硬件下沉执行」的应用侧表现）。

**⑥ 主流程编排**。`main` 用一对花括号限定 `SampleRes50ImagenetClassification` 的作用域，保证析构（资源释放）发生在进程退出前：

[examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp:193-225](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/src/sample_resnet50_imagenet_classification.cpp#L193-L225) —— 依次调用 `InitResource` → `PrepareModel("../model/resnet50.om")` → `Process()`，正是 4.2.2 那条 12 步链路的代码缩影。

#### 4.2.4 代码实践

**实践目标**：把「`atc` 编译命令」和「样例代码里的加载/执行步骤」两条线对上号（本讲规格指定的核心实践）。

**操作步骤**：

1. 阅读 [ResNet50 样例 README](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/README.md)，把「快速开始」里的 `atc` 命令抄出来。
2. 打开主程序源码，在 `InitResource` / `PrepareModel` / `Process` 三个函数里，分别找到对应的 ACL 接口调用。
3. 填写下表（参考答案已给）：

| 阶段 | `atc` 命令 / ACL 接口 | 作用 |
|------|----------------------|------|
| 编译模型 | `atc --model=...onnx --framework=5 --output=resnet50 --soc_version=... --input_format=NCHW --output_type=FP32` | ONNX → OM |
| 初始化 | `aclInit` | 初始化 ACL 环境 |
| 选设备 | `aclrtSetDevice(0)` | 选定 0 号卡 |
| 建上下文/流 | `aclrtCreateContext` / `aclrtCreateStream` | 创建 Context、Stream |
| 查内存 | `aclmdlQuerySize` | 查工作/权重内存大小 |
| 加载模型 | `aclmdlLoadFromFileWithMem` | 加载 OM，得 `modelId` |
| 取描述 | `aclmdlGetDesc` | 取输入输出元信息 |
| 搬输入 | `MemcpyFileToDeviceBuffer` | 图片 bin → Device |
| 执行 | `aclmdlExecute` | 同步推理 |
| 取输出 | `aclrtMemcpy`（device→host） | 结果拷回 Host |

**需要观察的现象**：编译阶段（`atc`）和执行阶段（ACL 接口）是完全分开的两组操作；执行阶段内部又有「初始化 → 加载 → 执行」的严格顺序，顺序错了会直接报错。

**预期结果**：你能指着源码说出「这一行对应 12 步里的第几步」。如果你手头有昇腾环境，可以按 README 的「快速开始」真正跑一遍，预期看到 `[INFO] SAMPLE PASSED` 以及两张图片的 Top-5 分类（161 对应 basset hound、267 对应 standard poodle）；**若没有设备，本实践作为源码阅读型实践完成即可，标注「待本地验证」的运行结果部分不必强求**。

#### 4.2.5 小练习与答案

**练习 1**：为什么样例在 `aclmdlLoadFromFileWithMem` 之前要先调一次 `aclmdlQuerySize`？
> **答案**：因为加载接口要求调用方**自带**两块 Device 内存（工作内存、权重内存），`aclmdlQuerySize` 用来查出这两块内存各需要多大，才能用 `aclrtMalloc` 预分配。这是一种「内存由用户管理」的设计，便于复用与控制峰值。

**练习 2**：`OutputModelResult` 里有一段 `if (!g_isDevice) { ... aclrtMemcpy ... }`，它在处理什么情况？
> **答案**：处理程序运行在 Host（而非 Device）的情况。此时模型输出在 Device 内存里，需要用 `aclrtMemcpy(..., ACL_MEMCPY_DEVICE_TO_HOST)` 把数据拷回 Host 才能在 CPU 侧解析 Top-5。这正是 u1-l1 讲的 Host/Device 数据搬运在代码里的体现。

**练习 3**：如果把样例从「单 Batch」改成「动态 Batch」，`atc` 命令要多加哪些参数？
> **答案**：参考动态 Batch 样例 [README:74-77](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/2_sample_resnet50_imagenet_classification_dynamic_batch/README.md#L74-L77)：需要加 `--input_shape="x:-1,3,224,224"`（Batch 维设为 `-1` 表示可变）和 `--dynamic_batch_size="1,2,4,8"`（支持的档位列表）。动态分档的内部原理会在单元 5 详讲。

---

### 4.3 LLM 样例入口

#### 4.3.1 概念说明

`3_sample_qwen_llm` 是一个大语言模型（Qwen）推理样例。它和 ResNet50 看似都是「加载 OM → 执行」，但 LLM 有自己的特点，`atc` 编译参数也更复杂，值得单独认识一下入口。

LLM 样例与 CV 样例的主要差异：

- **多输入**：不仅有 `input_ids`（输入 token 序列），还有大量 KV Cache 输入（`past_key_*`、`past_value_*`），输入数量远多于 ResNet50 的单输入。
- **显式指定输入 shape**：因为输入多、且部分维度需要固定，`atc` 编译时必须用 `--input_shape` 把每个输入的 shape 显式列出来。
- **精度相关参数**：大模型对精度敏感，常加 `--precision_mode=must_keep_origin_dtype`、`--op_select_implmode=high_precision` 等。

#### 4.3.2 核心流程

LLM 样例的运行流程骨架与 ResNet50 一致（下模型 → `atc` → 编译运行），区别集中在 `atc` 命令：

```text
下载 qwen.onnx（约 4GB+，文件较大）
        │
        ▼
atc 编译（必须带 --input_shape，逐个列出所有输入的 shape）
        │   还常带 --precision_mode / --op_select_implmode / --external_weight
        ▼
bash scripts/build.sh && bash scripts/run.sh
        │
        ▼
程序输出：预测的下一个 token id + KV Cache（present_*）
```

给定一段输入 token 序列，模型做一次前向，输出预测的下一个 token ID 以及更新后的 KV Cache。

#### 4.3.3 源码精读

LLM 样例 README 的 `atc` 命令长得多，核心是一长串 `--input_shape`：

[examples/acl/3_sample_qwen_llm/README.md:66-78](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/3_sample_qwen_llm/README.md#L66-L78) —— `--input_shape` 用分号串起所有输入的 shape，例如 `input_ids:1,512` 表示 batch=1、序列长 512；`past_key_0.key:1,2,512,64` 是某层 KV Cache 的 key 张量。同时带 `--precision_mode=must_keep_origin_dtype`、`--op_select_implmode=high_precision`、`--external_weight=0`、`--output_type=FP32`。

关键参数含义见参数表：

[examples/acl/3_sample_qwen_llm/README.md:81-86](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/3_sample_qwen_llm/README.md#L81-L86) —— `--precision_mode=must_keep_origin_dtype` 强制保持原始数据类型避免精度损失；`--op_select_implmode=high_precision` 优先保证精度；`--external_weight=0` 表示权重内嵌在 OM 里不分离存储。

从预期输出可以直观看到 LLM 与 CV 样例的差别：

[examples/acl/3_sample_qwen_llm/README.md:113-127](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/3_sample_qwen_llm/README.md#L113-L127) —— 输入有 `input_ids` 加一大堆 `past_key_*`/`past_value_*`（KV Cache），输出除了 logits（`/lm_head/MatMul...logits`，shape `1 512 151936`，词表很大）还有更新后的 `present_*`（新 KV Cache），最后打印 `predicted_token_id`。

> 一句话对比：ResNet50 是「单输入、单输出、固定 shape」；Qwen LLM 是「多输入（含 KV Cache）、多输出（含新 KV Cache）、需显式声明 shape、对精度敏感」。执行侧的 ACL 调用骨架两者一致，差异主要在编译参数和输入输出张量的数量。

#### 4.3.4 代码实践

**实践目标**：识别 LLM 样例与 CV 样例在 `atc` 编译上的差异。

**操作步骤**：

1. 打开 [Qwen LLM 样例 README](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/3_sample_qwen_llm/README.md)，找到「快速开始」里的 `atc` 命令。
2. 把它和 ResNet50 的 `atc` 命令并排对比，列出 Qwen 多出来的参数。
3. 数一数 `--input_shape` 里共有几个输入，分别是什么类型。

**需要观察的现象**：Qwen 的 `atc` 命令明显更长；`--input_shape` 是必填项；多了精度与权重相关参数。

**预期结果**：你能说出至少三个差异——①Qwen 必须显式写 `--input_shape`（ResNet50 不用）；②Qwen 带精度参数 `--precision_mode`、`--op_select_implmode`；③Qwen 输入数量多（1 个 token 输入 + 多组 KV Cache），ResNet50 只有 1 个输入。运行结果（`predicted_token_id` 等）**待本地验证**（模型约 4GB+，且需匹配的昇腾卡）。

#### 4.3.5 小练习与答案

**练习 1**：Qwen 样例的 `--external_weight=0` 是什么意思？如果改成 `1` 会怎样？
> **答案**：`0` 表示权重内嵌在 OM 文件里（自包含）；改成 `1` 表示权重外置分离存储，OM 本身变小但部署时需要额外带上权重文件。外置权重的细节会在单元 7、单元 9 详讲。

**练习 2**：为什么 LLM 样例几乎一定要加 `--precision_mode=must_keep_origin_dtype`？
> **答案**：大模型层数多、计算链长，中间算子若被随意提升或降低精度（例如 FP16↔FP32 转换）容易累积误差导致精度下降甚至溢出；`must_keep_origin_dtype` 强制保持原始 dtype，避免这类精度损失。

---

## 5. 综合实践

**任务**：画一张「ResNet50 样例端到端时序图」，把命令行准备阶段和程序内部 ACL 调用阶段串成一条完整的执行时间线，并标注每一步「发生在 Host 还是 Device」「需不需要昇腾卡」。

**要求**：

1. 横轴为时间，左侧标注「命令行阶段（下模型 / atc / 预处理 / build）」，右侧标注「程序运行阶段（InitResource / PrepareModel / Process）」。
2. 在每个 `atc` 与 ACL 调用旁，写出对应的命令或函数名（参考 4.2.4 的表格）。
3. 用不同颜色或标记区分 Host 侧操作（如 `atc` 编译、`aclrtMemcpy` 拷回）与 Device 侧操作（如 `aclmdlExecute` 执行、Device 内存分配）。
4. 在图上圈出「数据跨 Host↔Device 边界」的两次搬运，并说明为什么需要搬运（提示：联系 u1-l1 的 Host/Device 概念）。

**预期产出**：一张能体现「编译与执行分离」「数据在 Host/Device 间搬运」「OM 是连接编译侧与执行侧的产物」这三个关键认知的时序图。若手头有昇腾环境，可顺势按 README 真实运行一遍来验证你的时序图；否则作为源码阅读型综合实践完成即可。

> 这个任务把你在这讲学到的样例目录结构、`atc` 用法、ACL 生命周期、Host/Device 数据搬运全部串了起来，是进入下一单元（前端解析与编译源码）前最好的热身。

## 6. 本讲小结

- `examples/` 按「场景分目录」组织；ACL 运行时样例集中在 `examples/acl/`，用「数字前缀 + 场景」命名，由浅入深排列。
- 离线端到端骨架是：**下载模型 → `atc` 编译成 OM → 预处理输入 → 编译运行样例**；前两步在普通主机即可完成，印证了 GE「编译与执行分离」的设计。
- ResNet50 样例用 12 步 ACL 调用覆盖了完整的模型推理生命周期：`aclInit` → `setDevice/context/stream` → `querySize/malloc/loadFromFileWithMem/getDesc` → `malloc/memcpy/execute/memcpy`。
- `aclmdlLoadFromFileWithMem` 加载的正是 `atc` 产出的 OM，在代码层面把「GE Compiler 产出」与「GE Executor 执行」连成了闭环。
- 程序用 `aclrtGetRunMode` 判断 Host/Device，据此决定取输出时是否要把数据从 Device 拷回 Host——这是 u1-l1 的 Host/Device 概念在代码里的落地。
- LLM（Qwen）样例与 CV 样例执行骨架相同，差异主要在 `atc`：必须显式 `--input_shape`、输入含大量 KV Cache、且更关注精度参数。

## 7. 下一步学习建议

本讲你把 GE 的**离线使用方式**整体跑通了，但一直把 `atc` 当黑盒。接下来建议：

- **进入单元 2（基石）**：先学 AscendIR 的 Graph/Node/OpDesc/Tensor 四层对象模型——这是 `atc` 内部把模型「翻译」成的核心数据结构，理解了它，才能看懂后续所有源码。
- **进入单元 3（前端接入与解析）**：重点看 [u3-l1 解析器框架](#) 和 [u3-l3 ATC 离线编译工具链](#)，把本讲里的 `atc` 命令对应到它的源码主流程（`main_impl` → `omg`），看清 ONNX 是怎么被解析成 AscendIR 的。
- **如果想先动手改样例**：可以尝试在 ResNet50 样例基础上，把它从单 Batch 改成动态 Batch（参考 4.2.5 练习 3），亲手感受 `--dynamic_batch_size` 带来的运行时档位切换，为单元 5 的动态分档埋下伏笔。
