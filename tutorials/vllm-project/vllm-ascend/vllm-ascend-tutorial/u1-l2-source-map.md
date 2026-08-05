# 源码地图：目录结构与模块总览

## 1. 本讲目标

本讲承接上一讲「vLLM Ascend 是什么」。你已经知道 `vllm-ascend` 是一个把 vLLM 运行到昇腾 NPU 上的**硬件插件**，它通过「可插拔硬件接口 + 大量打补丁（Patch）+ 继承重写 + 自定义算子」把上游 CUDA/CPU 路径改道指向 NPU。

但「打补丁」「自定义算子」这些词，到底落在哪些文件里？当你要找一个行为、改一处逻辑时，该进哪个目录？

学完本讲，你将能够：

1. 画出 `vllm-ascend` 仓库的顶层布局，区分**纯 Python 包**、**C++/AscendC 内核**、**工程配套**三类代码。
2. 记住 `vllm_ascend/` 下每一个一级子目录的职责，能在 30 秒内定位到「平台、补丁、Worker、注意力、算子、分布式、编译」这些核心模块。
3. 找到三个最关键的顶层文件：`vllm_ascend/__init__.py`（插件入口）、`vllm_ascend/platform.py`（平台核心）、`vllm_ascend/ascend_config.py`（配置对象）、`vllm_ascend/utils.py`（工具与补丁分发）。
4. 说清楚 vLLM 是如何通过 `setup.py` 里的 **entry points（入口点）** 发现并加载这个插件的。

本讲**只画地图**，不深入任何模块的实现细节——那些留给后续单元。先把「哪个目录管什么事」刻进脑子里，后面读源码才不会迷路。

## 2. 前置知识

读本讲前，你需要先建立以下几个直觉（来自上一讲）：

- **硬件插件（Hardware Pluggable）**：vLLM 不把硬件相关代码写死，而是留出接口，让外部包（如 `vllm-ascend`）来填。这样上游 vLLM 可以独立升级，插件单独维护。
- **NPUPlatform**：`vllm-ascend` 提供给 vLLM 的「平台」实现，告诉 vLLM「我这是一张昇腾卡，设备能力、注意力后端、编译后端都用我这套」。
- **CANN / torch-npu**：昇腾的软件栈。CANN 是底层驱动与算子库，`torch-npu` 是让 PyTorch 能调用 NPU 的桥接层。它们和 vLLM、插件都有**精确版本**对应关系。

此外补充一个对本讲很有用的概念：

- **entry points（入口点）**：这是 Python 打包标准里的机制。一个包可以在自己的元数据里登记若干「入口」，形如 `组名 = 模块:函数`。别的程序（这里是 vLLM）在启动时去扫描某个组，就能找到并调用插件登记好的函数。`vllm-ascend` 正是通过这个机制被 vLLM 自动发现、加载的——你不需要手动 `import vllm_ascend`。

> 术语提示：本讲提到的「目录」和「模块」会混用。在 Python 语境里，一个含 `__init__.py` 的目录本身也是一个「包（package）」，所以「`patch/` 模块」和「`patch/` 目录」是一回事。

## 3. 本讲源码地图

本讲只读以下文件/目录的**结构**和**头部**，不深入实现：

| 文件 / 目录 | 作用 | 本讲用它来 |
| --- | --- | --- |
| `vllm_ascend/__init__.py` | 插件入口，定义 `register()` 等回调 | 讲插件如何被发现、加载 |
| `setup.py` | 打包构建脚本，含 `entry_points` | 讲入口点登记 |
| `pyproject.toml` | 项目元信息、依赖、lint/测试配置 | 讲构建依赖与版本锁定 |
| `vllm_ascend/platform.py` | `NPUPlatform` 平台核心类 | 定位平台代码 |
| `vllm_ascend/ascend_config.py` | `AscendConfig` 配置对象 | 定位配置代码 |
| `vllm_ascend/utils.py` | 工具函数与 `adapt_patch` 补丁分发 | 定位工具/补丁入口 |
| `vllm_ascend/`（各子目录） | 核心功能模块 | 画目录树、标注职责 |
| `csrc/` | C++/AscendC 算子内核 | 区分 Python 层与内核层 |

## 4. 核心概念与源码讲解

### 4.1 目录结构总览：仓库的三层代码组织

#### 4.1.1 概念说明

第一次打开 `vllm-ascend` 仓库，你会看到几十个文件和目录。别慌，它们其实可以分成**三大类**：

1. **纯 Python 包**：`vllm_ascend/`，这是插件的主体，所有「打补丁、继承重写、平台对接」逻辑都在这里。它是你 90% 时间会待的地方。
2. **C++/AscendC 内核**：`csrc/`，用 C++ 和昇腾特有的 AscendC 语言写的**高性能算子内核**（attention、moe、通信等）。Python 层会通过 PyTorch 的算子注册机制调用它们。
3. **工程配套**：构建脚本（`setup.py`、`CMakeLists.txt`）、依赖清单（`requirements*.txt`）、文档（`docs/`）、示例（`examples/`）、测试（`tests/`）、CI 配置（`.github/`）、Dockerfile 等。这些不参与推理逻辑，但决定了项目怎么构建、怎么测、怎么跑。

理解这三类的分工，是建立源码地图的第一步：**找逻辑进 `vllm_ascend/`，找极致性能进 `csrc/`，找怎么用进 `examples/` 和 `docs/`。**

#### 4.1.2 核心流程

当你拿到仓库后，建立心智模型的顺序是：

```
看 README（项目定位）
   ↓
看 vllm_ascend/（主体逻辑在哪）
   ↓
看 csrc/（性能关键路径在哪）
   ↓
看 examples/ + tests/（怎么跑、怎么验）
   ↓
看 setup.py + CMakeLists.txt（怎么编译连起来）
```

本讲负责前三步中的「看目录」，让你对每一类代码的存放位置有整体印象。

#### 4.1.3 源码精读

仓库顶层（节选关键条目）大致长这样：

```
vllm-project-vllm-ascend/
├── vllm_ascend/        # ① 纯 Python 包：插件主体
├── csrc/               # ② C++/AscendC 内核
├── examples/           # ③ 示例（离线推理、在线服务等）
├── tests/              # ③ 测试（ut / e2e）
├── docs/               # ③ 文档
├── benchmarks/         # ③ 性能基准
├── tools/              # ③ 辅助脚本
├── setup.py            # ③ Python 打包 + entry points
├── CMakeLists.txt      # ③ 顶层 CMake（驱动 csrc 编译）
├── requirements.txt    # ③ 运行依赖
├── pyproject.toml      # ③ 构建依赖 + lint/测试配置
└── README.md / README.zh.md
```

其中 `csrc/` 的顶层目录反映了内核按功能分类的组织方式，下面摘录一部分真实存在的内核目录：

```
csrc/
├── attention/            # 各类注意力内核（含稀疏/索引注意力）
├── moe/                  # MoE 相关内核（因果卷积、chunk 前向等）
├── mc2/                  # MC2 通信内核（MoE 的 alltoall 通信）
├── mla_preprocess/       # MLA 注意力预处理
├── gmm/                  # 分组矩阵乘（grouped matmul，MoE 专家计算）
├── common/               # 公共头/工具
├── aclnn_torch_adapter/  # 把 AscendC 算子适配成 torch 算子的桥接
├── CMakeLists.txt        # 收集所有 KERNEL_FILES 并链接 CANN
└── torch_binding.cpp     # Python 侧绑定入口
```

每一个具体算子目录（例如 `csrc/moe/causal_conv1d/`）内部都遵循昇腾 AscendC 算子的**双文件结构**：`op_host/`（主机侧：算子形状推导、数据类型推导、tiling 切分策略）+ `op_kernel/`（设备侧：真正在 NPU 上执行的内核代码），外加一个 `CMakeLists.txt`：

```
csrc/moe/causal_conv1d/
├── CMakeLists.txt
├── op_host/      # 主机侧：shape/tiling 推导
└── op_kernel/    # 设备侧：NPU 内核实现
```

> 这是 AscendC 算子的通用约定，记住「`op_host` 管推导、`op_kernel` 管执行」即可。细节留到 u6-l3 讲。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，无需 NPU：

1. **实践目标**：亲手把仓库的三类代码分清楚。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -1`，对照上面给出的顶层布局。
   - 执行 `ls -1 csrc/`，数一数有多少个功能目录（attention / moe / mc2 / mla_preprocess / gmm …）。
   - 进入任意一个算子目录，例如 `ls -1 csrc/moe/causal_conv1d/`，确认它有 `op_host/` 和 `op_kernel/` 两个子目录。
3. **需要观察的现象**：`vllm_ascend/` 是纯 Python；`csrc/` 里全是 `.cpp/.h` 和 `CMakeLists.txt`；`examples/`、`tests/`、`docs/` 各司其职。
4. **预期结果**：你能在不看本讲义的情况下，说出「要改一个补丁逻辑去哪、要改一个高性能算子去哪、要找运行示例去哪」。
5. 若你拿不到真实仓库环境，以上命令的输出在本讲「源码精读」一节已给出，可对照阅读——其余现象「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vllm-ascend` 要把高性能算子用 C++/AscendC 写在 `csrc/`，而不是全用 Python？

**参考答案**：因为这部分代码需要在 NPU 上以接近硬件极限的性能执行（如 attention、MoE 通信），Python 解释执行无法满足性能与硬件特性（如 tiling、共享内存）的需求；AscendC 能直接操控 NPU 硬件资源。Python 层只负责调度与对接 vLLM，真正的数值计算下沉到 `csrc/`。

**练习 2**：`csrc/` 下一个算子目录（如 `csrc/moe/causal_conv1d/`）里的 `op_host/` 和 `op_kernel/` 各自承担什么职责？

**参考答案**：`op_host/` 负责**主机侧**的形状推导、数据类型推导和 tiling（数据切块）策略；`op_kernel/` 负责**设备侧**真正在 NPU 上跑的内核实现。两者合起来才是一个完整的 AscendC 自定义算子。

### 4.2 模块划分：vllm_ascend/ 一级目录职责速查

#### 4.2.1 概念说明

`vllm_ascend/` 是插件主体，里面有 20 多个一级子目录和几个顶层 `.py` 文件。直接把它们全记下来很难，但如果按**职能**归类，就清晰多了。我们可以把它们分成几组：

- **平台与配置组**：定义「我是谁」「我该怎么配置」。
- **集成核心组（Patch）**：本插件最核心的武器——通过打补丁把上游 vLLM 的路径改道到 NPU。
- **执行主链路组（Worker）**：真正跑一次推理时，输入怎么准备、模型怎么前向、结果怎么采样。
- **算力组（attention / ops / lora / quantization）**：注意力、自定义算子、LoRA、量化这些「具体怎么算」的实现。
- **分布式组（distributed / eplb）**：多卡并行、通信、专家负载均衡。
- **编译组（compilation）**：把计算图编译/融合，启用 ACL Graph（NPU 版的图捕获）。
- **模型与加载组（models / model_loader）**：少数需要直接实现的模型，以及各种权重加载器。
- **进阶特性组（spec_decode / kv_offload / xlite / _310p …）**：投机解码、KV 卸载、分层推理、310P 适配等。

#### 4.2.2 核心流程

下面这张表是本讲最重要的一张「地图」。建议你对照真实目录阅读，把它当成查阅手册。

| 一级目录 / 文件 | 职责（一句话） |
| --- | --- |
| `__init__.py` | **插件入口**：定义 `register()` 等回调，供 vLLM 发现加载 |
| `platform.py` | **平台核心**：`NPUPlatform`，重写设备能力、注意力后端、编译后端等钩子 |
| `ascend_config.py` | **配置对象**：`AscendConfig`，解析 `--additional-config` 传入的 JSON |
| `ascend_forward_context.py` | **前向上下文**：注入运行期信息（如 MoE 通信类型） |
| `envs.py` | **环境变量**：集中管理所有 `VLLM_ASCEND_*` 环境变量 |
| `utils.py` | **工具集 + 补丁分发**：含 `adapt_patch()`（两阶段打补丁入口） |
| `logger.py` | 日志封装 |
| `patch/` | **集成核心**：所有对上游 vLLM 的 Monkey-patch（分 platform / worker 两阶段） |
| `worker/` | **执行主链路**：`NPUWorker`、`NPUModelRunner`（v1 与 v2） |
| `attention/` | **注意力后端**：Ascend 注意力、MLA / SFA / DSA、上下文并行 |
| `ops/` | **自定义算子**：Python 算子注册 + Triton 内核 + FusedMoE 引擎 |
| `distributed/` | **分布式**：并行组、HCCL 通信器、KV 传输、在线权重传输 |
| `compilation/` | **图编译**：编译接口、融合 Pass、ACL Graph |
| `models/` | 少数**直接实现**的模型/层（如 deepseek_v4、minimax_m3） |
| `model_loader/` | **权重加载器**：默认加载、RFork（进程 fork 加速）、Netloader（弹性加载） |
| `quantization/` | **量化方法**：w8a8 / w4a8 / w4a4 / fp8 等 NPU 量化实现 |
| `lora/` | **LoRA**：NPU LoRA 算子与 punica 集成 |
| `spec_decode/` | **投机解码**：eagle / ngram / mtp 等 proposer |
| `eplb/` | **专家负载均衡**：运行期把 MoE 专家在多卡间重均衡 |
| `kv_offload/`、`simple_kv_offload/` | **KV 卸载**：CPU↔NPU 间的 KV cache 搬运 |
| `device_allocator/` | **显存分配**：睡眠模式下的显存管理 |
| `sample/` | **采样器**：AscendSampler、拒绝采样、penalties |
| `profiler/`、`profiling_config.py` | 性能采集配置 |
| `_310p/` | **310P 适配**：310P 硬件专属的 worker / runner / ops / 量化 |
| `xlite/` | **分层推理**：XLite 分块执行 |
| `core/`、`model_executor/`、`device/` | 少量核心抽象与设备封装 |

> 提示：上表中标注**加粗**的几项（`__init__.py`、`platform.py`、`patch/`、`worker/`、`ops/`）是后续整个学习路线的主干，其余多为特性分支。新手优先记住加粗项。

#### 4.2.3 源码精读

你可以用一条命令亲眼看到这张表对应的所有一级条目（输出已在本讲准备阶段实际执行过）：

```
ls -1 vllm_ascend/
```

它会列出 `_310p`、`ascend_config.py`、`ascend_forward_context.py`、`attention`、`compilation`、`core`、`device`、`device_allocator`、`distributed`、`envs.py`、`eplb`、`kv_offload`、`logger.py`、`lora`、`memcache_comm_fence.py`、`meta_registration.py`、`model_executor`、`model_loader`、`models`、`ops`、`patch`、`platform.py`、`profiler`、`profiling_config.py`、`quantization`、`sample`、`simple_kv_offload`、`spec_decode`、`utils.py`、`worker`、`xlite` 等条目——与上表一一对应。

`patch/` 内部进一步分为两个阶段，这也是本插件最重要的组织原则之一（详情见 u3）：

```
vllm_ascend/patch/
├── __init__.py
├── platform/   # 平台级补丁：在引擎核心子进程生效（影响调度/分布式/MoE/KV）
└── worker/     # Worker 级补丁：在每个 worker 生效（影响模型前向/算子/图）
```

`ops/` 内部则体现了「算子三层体系」（详情见 u6）：

```
vllm_ascend/ops/
├── __init__.py            # 算子总入口
├── register_custom_ops.py # Python 算子注册
├── triton/                # Triton 内核（rmsnorm / rope / fla / mamba …）
├── fused_moe/             # FusedMoE 引擎（AscendMoERunner 全链路）
└── ...                    # 各类功能性算子（linear / layernorm / dsa / mla …）
```

#### 4.2.4 代码实践

这就是本讲规格要求的实践任务：

1. **实践目标**：亲手画一张 `vllm_ascend/` 的二级目录树，并给每个一级子目录标注一句话职责。
2. **操作步骤**：
   - 执行 `ls -1 vllm_ascend/` 得到一级条目。
   - 对你感兴趣的一级目录（如 `patch/`、`worker/`、`ops/`、`attention/`、`distributed/`）再执行一次 `ls -1 vllm_ascend/<目录>/`，得到二级结构。
   - 用文本或画图工具整理成一棵树，每个一级目录后面跟一句话职责（直接抄本节职责表即可）。
3. **需要观察的现象**：你会发现 `patch/` 只有 `platform/` 和 `worker/` 两个子目录；`worker/` 里有 `v2/`（新架构）和 `model_runner_v1.py`（旧架构）；`ops/` 下 `triton/` 和 `fused_moe/` 是两个重头戏。
4. **预期结果**：得到一张可以贴在显示器旁的速查图。后续读任何一讲，你都能迅速定位到对应目录。
5. 这一步纯靠 `ls` 即可完成，不需要 NPU；若手边没有仓库，可基于本节给出的真实目录结构完成绘制。

#### 4.2.5 小练习与答案

**练习 1**：`patch/platform/` 和 `patch/worker/` 的区别是什么？

**参考答案**：`patch/platform/` 是**平台级补丁**，在引擎核心子进程生效，影响调度器、分布式、MoE 工厂、KV cache 等「全局/共享」行为；`patch/worker/` 是 **Worker 级补丁**，在每个推理 worker 子进程生效，影响模型前向、算子替换、图模式等「单卡执行」行为。两者对应 `adapt_patch()` 的两个阶段（见 4.3）。

**练习 2**：如果你想新增一个针对某模型的「前向计算改写」，应该放进哪个目录？

**参考答案**：通常放进 `vllm_ascend/patch/worker/`（因为前向发生在 worker 子进程），并以 `patch_<模型名>.py` 命名；如果是平台级、影响调度的改写，才放进 `patch/platform/`。命名与文档规范见 u11-l5。

### 4.3 关键顶层文件与插件入口发现

#### 4.3.1 概念说明

除了子目录，`vllm_ascend/` 根下还有几个**顶层 `.py` 文件**格外重要，它们是整个插件的「枢纽」：

- **`__init__.py`**：包的入口。vLLM 通过 entry points 找到的 `register()` 就定义在这里。它在被 import 时还会做一些兼容性处理（比如给 triton 打桩）。
- **`platform.py`**：定义 `NPUPlatform`，这是插件交给 vLLM 的「平台身份证」。
- **`ascend_config.py`**：定义 `AscendConfig`，承载所有 Ascend 专属配置。
- **`utils.py`**：大量工具函数，其中 `adapt_patch()` 是**两阶段打补丁的总入口**，是理解整个插件如何改造 vLLM 的关键。

#### 4.3.2 核心流程

vLLM 启动 → 发现并加载插件的链路如下：

```
vLLM 启动
   ↓
读取已安装包的 entry_points，发现组 "vllm.platform_plugins"
   ↓
找到登记项 "ascend = vllm_ascend:register"
   ↓
import vllm_ascend（触发 __init__.py 顶部的兼容性处理）
   ↓
调用 vllm_ascend.register()
   ↓
返回字符串 "vllm_ascend.platform.NPUPlatform"
   ↓
vLLM 把 NPUPlatform 选为当前平台
```

与此同时，`vllm.general_plugins` 组里的另外几个入口（`register_connector`、`register_model_loader`、`register_service_profiling`、`register_model`）会在更晚的时机被调用，分别注册 KV 连接器、模型加载器、性能采集、自定义模型。这些回调大多会先调用 `_ensure_global_patch()` 确保**平台级补丁**已经打上。

#### 4.3.3 源码精读

**① 插件入口点登记在 `setup.py`**：`vllm.platform_plugins` 组里登记了 `ascend = vllm_ascend:register`，另外四个挂在 `vllm.general_plugins` 组下。

参见 [setup.py:543-549](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L543-L549) ——这段代码声明了 vLLM 用来发现本插件的所有入口点。

**② `register()` 返回平台路径**：它只做一件事——告诉 vLLM「我的平台类是这个字符串指向的类」。

参见 [vllm_ascend/__init__.py:73-76](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L73-L76) ——`register()` 返回 `"vllm_ascend.platform.NPUPlatform"`。

**③ import 时的兼容性打桩**：`__init__.py` 在被 import 的瞬间（早于任何业务代码）就给 `triton.experimental.gluon` 等模块打上空桩，让依赖新版 triton 的上游 vLLM 能在 triton-ascend 旧版本上跑起来。

参见 [vllm_ascend/__init__.py:22-51](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L22-L51) ——这是「main2main 兼容」的关键：用 `ModuleType` 占位，避免 import 失败。

**④ `_ensure_global_patch()` 保证平台级补丁只打一次**：用一个全局标志 `_GLOBAL_PATCH_APPLIED` 防止重复打补丁，内部调用 `utils.adapt_patch(is_global_patch=True)`。

参见 [vllm_ascend/__init__.py:56-70](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L56-L70) ——这是平台级补丁的「一次性闸门」。

**⑤ `adapt_patch()` 的两阶段分发**：根据 `is_global_patch` 决定 import `patch.platform`（平台级）还是 `patch.worker`（worker 级）。

参见 [vllm_ascend/utils.py:533-537](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L533-L537) ——整个插件「打补丁」的总开关就这几行：靠 import 的副作用触发补丁应用。

**⑥ `NPUPlatform` 类声明**：继承自 vLLM 的 `Platform`。

参见 [vllm_ascend/platform.py:127](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L127) ——`class NPUPlatform(Platform):` 是平台核心的起点（它的具体钩子方法留到 u2-l1 精读）。

**⑦ `AscendConfig` 解析 additional_config**：把 vLLM 传来的 JSON 拆成若干子配置对象。

参见 [vllm_ascend/ascend_config.py:27-57](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L27-L57) ——`AscendConfig.__init__` 逐项解析 `xlite_graph_config`、`ascend_compilation_config`、`eplb_config` 等子配置（细节留到 u2-l2）。

#### 4.3.4 代码实践

这是一个**调用链追踪型实践**，无需 NPU：

1. **实践目标**：把「vLLM 启动 → NPUPlatform 被选中」这条链路在源码里走一遍。
2. **操作步骤**：
   - 打开 [setup.py:543-549](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/setup.py#L543-L549)，确认入口点登记。
   - 跳到 [vllm_ascend/__init__.py:73-76](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L73-L76)，看 `register()` 返回的字符串。
   - 跳到 [vllm_ascend/platform.py:127](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L127)，确认 `NPUPlatform` 类确实存在。
3. **需要观察的现象**：`register()` 不直接 `import` 平台类，而是返回一个**字符串路径**——vLLM 拿到字符串后自己去 import，这是一种延迟加载、避免循环依赖的常见手法。
4. **预期结果**：你能用自己的话讲清楚「entry_points → register() → 字符串路径 → vLLM import NPUPlatform」这条链，并解释为什么用字符串而非直接返回类。
5. 该实践只读源码、不执行推理，因此不依赖 NPU；如果你能运行 vLLM，可在日志中观察到平台被选中的相关输出——具体日志文案「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`register()` 为什么返回字符串 `"vllm_ascend.platform.NPUPlatform"`，而不是直接 `return NPUPlatform`？

**参考答案**：用字符串路径可以让 vLLM 在需要时才**延迟 import** 平台类，避免在插件加载早期就触发一长串 import（可能造成循环依赖或拖慢启动）。这也是 vLLM 可插拔硬件接口的约定写法。

**练习 2**：`_ensure_global_patch()` 里的 `_GLOBAL_PATCH_APPLIED` 标志有什么用？

**参考答案**：它保证**平台级补丁在每个进程里只被应用一次**。因为 vLLM 有多个插件回调（`register_connector`、`register_model_loader` 等）都会调用 `_ensure_global_patch()`，若不加保护会重复打补丁、覆盖已修改的对象，引发难以排查的错误。标志位把多次调用收敛为一次实际执行。

**练习 3**：`adapt_patch(is_global_patch=True)` 和 `adapt_patch(is_global_patch=False)` 分别触发哪类补丁？

**参考答案**：前者 `import vllm_ascend.patch.platform`，触发**平台级补丁**（在引擎核心生效）；后者 `import vllm_ascend.patch.worker`，触发 **worker 级补丁**（在每个推理 worker 生效）。两者靠 Python 模块 import 的副作用来应用补丁。

## 5. 综合实践

把本讲的三块知识串起来，完成一个「仓库导览小任务」：

1. **画出完整的二级目录树**：从仓库根出发，至少展开到 `vllm_ascend/` 的二级目录（即 `vllm_ascend/patch/platform`、`vllm_ascend/worker/v2`、`vllm_ascend/ops/triton` 这一层）。
2. **给三类代码上色/标注**：用三种标记区分 ① 纯 Python 包、② C++/AscendC 内核、③ 工程配套。
3. **标出「主干五件套」**：在树上特别标记 `__init__.py`、`platform.py`、`patch/`、`worker/`、`ops/`，并各写一句话职责。
4. **补一条「发现链路」**：在图旁用箭头画出 `setup.py entry_points → __init__.register() → "vllm_ascend.platform.NPUPlatform" → NPUPlatform 类` 这条链，并注明 `_ensure_global_patch()` 在哪一步被调用。

完成后，这张图就是你阅读后续所有讲义的「导航仪」。每次进入一个新主题（比如 u3 讲 Patch、u4 讲 Worker），先在这张图上找到对应目录，再开始读源码，方向感会强很多。

## 6. 本讲小结

- `vllm-ascend` 仓库的代码分三类：**纯 Python 包** `vllm_ascend/`、**C++/AscendC 内核** `csrc/`、**工程配套**（setup/CMake/examples/tests/docs）。
- `vllm_ascend/` 的几十个一级目录可按职能分组：**平台与配置**（`platform.py`、`ascend_config.py`、`envs.py`）、**集成核心**（`patch/`）、**执行主链路**（`worker/`）、**算力**（`attention/`、`ops/`、`lora/`、`quantization/`）、**分布式**（`distributed/`、`eplb/`）、**编译**（`compilation/`）、**模型与加载**（`models/`、`model_loader/`）、**进阶特性**（`spec_decode/`、`kv_offload/`、`xlite/`、`_310p/`）。
- 最关键的顶层文件是 `__init__.py`（入口）、`platform.py`（`NPUPlatform`）、`ascend_config.py`（`AscendConfig`）、`utils.py`（含 `adapt_patch`）。
- `patch/` 分 `platform/`（全局）和 `worker/`（单卡）两阶段；`ops/` 分 Python 注册、Triton 内核、FusedMoE 引擎三层——这是后续单元的主干结构。
- vLLM 通过 `setup.py` 的 **entry points** 发现插件：`vllm.platform_plugins` 组里的 `ascend = vllm_ascend:register` 触发 `register()`，后者返回 `NPUPlatform` 的字符串路径。
- `adapt_patch()` 是「打补丁」的总开关，靠 import 的副作用分别应用平台级与 worker 级补丁。

## 7. 下一步学习建议

本讲只画了地图，没有进入任何模块内部。建议按以下顺序深入：

1. **先读 u1-l3《环境准备与安装构建》**：弄清楚 `setup.py` + `CMakeLists.txt` 如何把 `csrc/` 编译成可被 Python 调用的算子库，以及 `envs.py` 里的构建相关环境变量。
2. **再读 u1-l5《插件入口：注册机制与发现流程》**：把本讲 4.3 的发现链路彻底讲透，包括 `_ensure_global_patch` 与 triton 兼容桩的细节。
3. **建立全局印象后**，从 u2《平台层与配置体系》开始正式进入模块内部，先吃透 `NPUPlatform` 与 `AscendConfig`，再进入 u3《Patch 机制》这个本插件的核心。

在读后续讲义时，请随时回到本讲的「目录职责速查表」对号入座——它就是你的源码导航仪。
