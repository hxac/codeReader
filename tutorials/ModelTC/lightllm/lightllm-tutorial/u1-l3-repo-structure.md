# 仓库目录结构与代码组织

## 1. 本讲目标

前两讲我们知道了 LightLLM 是什么、怎么安装和启动。但在真正读源码之前，还有一个更基础的能力要先建立：**看懂仓库是怎么摆放代码的**。

一个推理框架有几十个模型、好几条进程、成百上千个文件，如果不知道「哪类功能放在哪个目录」，读源码就会像在迷宫里乱撞。本讲的目标是：

- 看懂 LightLLM 仓库的**顶层目录**各自负责什么。
- 理解 `lightllm` 这个 Python 包内部 `server / common / models / distributed / utils` 五大子模块的**职责划分**。
- 能够在看到任何一个功能点（比如「调度循环」「注意力算子」「新增模型」）时，**快速定位**到它对应的源码目录。
- 亲手画出 `lightllm/server` 与 `lightllm/common` 的二级目录树，建立一张可以长期使用的「代码地图」。

学完本讲，你拿到任何一个 PR 或 issue，都能第一时间判断该去哪个目录看代码。

## 2. 前置知识

本讲假定你已经读过 **u1-l1（项目总览）** 和 **u1-l2（安装与快速启动）**，了解：

- LightLLM 是一个纯 Python 的 LLM 推理与服务框架（[README.md:18](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/README.md#L18) 这一句话定义了它的定位）。
- 它采用**多进程架构**：HttpServer、Router、ModelBackend、Detokenization 等是独立进程，通过 zmq、rpyc、共享内存协作。
- 它主打 **token 级 KV Cache 管理**和**易扩展**（一个新模型只需在 `lightllm/models/` 下加一个目录）。

本讲会用到几个很基础的概念，先简单解释：

- **Python 包（package）**：一个含有 `__init__.py` 的目录，可以被 `import`。LightLLM 的所有运行时代码都放在 `lightllm/` 这个包里。
- **顶层目录 vs 包内目录**：仓库根目录下的 `docs/`、`test/`、`docker/` 等是「顶层目录」，它们大多不参与运行（文档、测试、部署脚本）；而 `lightllm/` 才是真正被打包安装、被 `import` 的代码。
- **入口文件（entry）**：程序启动时第一个被执行的文件，比如 `python -m lightllm.server.api_server` 中的 `api_server.py`。

## 3. 本讲源码地图

本讲关注的是「目录与包的组织方式」，因此精读的文件不多，重点是把目录看清楚。下面列出本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目说明，定义 LightLLM 的定位与生态。 |
| `setup.py` | 打包配置，告诉我们「哪些目录是真正的包」「依赖是什么」。 |
| `lightllm/__init__.py` | `lightllm` 包的入口文件，内容极简。 |

围绕这三个文件，我们会展开看两个目录：仓库**顶层目录**与 **`lightllm` 包内部目录**。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

1. **仓库顶层目录结构** —— 仓库根目录下都有什么，哪些是代码、哪些是辅助。
2. **`lightllm` 包的内部组织** —— 运行时代码是如何按职责切分到五大子包的。

### 4.1 仓库顶层目录结构

#### 4.1.1 概念说明

一个成熟的 Python 项目，仓库根目录通常不会把所有东西都堆在一起，而是按「用途」划分：

- **运行时代码**：真正被打包、被 `import`、被运行的代码（这里是 `lightllm/`）。
- **构建与依赖声明**：`setup.py`、`requirements.txt`，告诉 pip「怎么装」。
- **文档**：`docs/`。
- **测试**：`test/`、`unit_tests/`。
- **部署辅助**：`docker/`、`tools/`、构建脚本。
- **示例**：`demos/`。
- **项目元信息**：`README.md`、`LICENSE`、`CONTRIBUTING.md`。

理解这套约定后，你拿到任何一个开源仓库都能快速分类，而不必逐个打开看。

#### 4.1.2 核心流程

定位一个功能时，可以按下面的顺序找目录：

1. **先看根目录有没有 README / docs** → 了解项目定位。
2. **看 `setup.py` / `requirements.txt`** → 确认运行时主包叫什么、依赖是什么。
3. **进入运行时主包**（`lightllm/`）→ 按「功能域」找子目录。
4. **需要复现/测试时**再去 `test/`、`unit_tests/`、`demos/`。

这套流程的核心思想是：**把仓库分成「非运行时辅助」和「运行时包」两大区，先认路再进包。**

#### 4.1.3 源码精读

先看打包配置，它最直接地告诉我们「仓库里到底哪些是包」：

```python
package_data = {"lightllm": ["common/all_kernel_configs/*/*.json", "common/triton_utils/*/*/*/*/*.json"]}
setup(
    name="lightllm",
    ...
    packages=find_packages(exclude=("build", "include", "test", "dist", "docs", "benchmarks", "lightllm.egg-info")),
```

这是 [setup.py:3-7](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L3-L7)。关键点有两个：

- `name="lightllm"` 说明打包后的包名就是 `lightllm`，也就是根目录下的 `lightllm/` 目录。
- `find_packages(...)` 会自动发现所有含 `__init__.py` 的子目录作为包；它 `exclude` 掉了 `test`、`docs`、`build` 等——**这等于官方声明：这些目录不是运行时代码**，安装时不会被打进包里。

另外，[setup.py:3](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L3) 的 `package_data` 还特别把两类 **JSON 配置文件**（kernel 配置、triton 工具配置）声明为需要随包分发的数据。这说明：即便不是 `.py`，只要运行时要用，也得显式声明打包——这是读包组织时的一个细节。

> 说明：`setup.py` 里的 `install_requires`（[setup.py:19-30](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/setup.py#L19-L30)）和版本号已在 u1-l1 讲过，这里不重复，我们只关注它对**目录/包**的声明。

结合 `find_packages` 的排除列表，我们可以把仓库顶层目录分成三类：

```
ModelTC-lightllm/                  # 仓库根目录
├── lightllm/                      # ★ 运行时主包（被打包安装）
├── docs/                          # 文档（CN/EN 双语，非运行时）
├── test/                          # 集成测试 / benchmark / 启动脚本
├── unit_tests/                    # 单元测试（按 common/models/server/utils 组织）
├── demos/                         # 示例（如 qa_server 聊天 demo）
├── docker/                        # Dockerfile + 构建脚本
├── tools/                         # 辅助工具（如 quick_launch_docker.py）
├── assets/                        # logo 等静态资源
├── README.md                      # 项目说明
├── setup.py                       # 打包配置
├── requirements.txt               # 完整锁版依赖
├── LICENSE / CONTRIBUTING.md      # 许可证与贡献指南
├── benchmark.md / format.py       # 基准说明 / 代码格式化脚本
└── .github/                       # CI 配置（github actions）
```

几个值得注意的点：

- **`test/` 和 `unit_tests/` 并存**：`test/` 偏向集成/性能/启动脚本（里面有 `benchmark/`、`start_scripts/`、`test_api/` 等子目录），`unit_tests/` 则按 `common/models/server/utils` 四块组织单元测试——这个划分恰好和 `lightllm` 包内的子模块对应，是个有用的线索。
- **`docs/` 分 `CN/` 和 `EN/`**：中英两套文档，互相对应。你之后会经常来这里查 API 说明。
- **`demos/qa_server/`**：一个最小可跑的聊天机器人示例，适合作为「读源码的起点」。

#### 4.1.4 代码实践

**实践目标**：用命令验证「哪些顶层目录是真正的包，哪些不是」。

**操作步骤**：

1. 在仓库根目录执行 `python -c "from setuptools import find_packages; print(find_packages(exclude=('build','include','test','dist','docs','benchmarks','lightllm.egg-info')))"`，观察输出。
2. 把输出里的包名前缀和顶层目录对照：你会发现输出的包名全部以 `lightllm.` 开头，**没有** `docs.*`、`test.*`、`demos.*`。

**需要观察的现象**：

- 输出是一长串 `lightllm`、`lightllm.server`、`lightllm.common`、`lightllm.models.xxx` 等包名。
- `docs/`、`test/`、`docker/`、`tools/`、`demos/` 都**不会**出现在列表里。

**预期结果**：直观证明「仓库里只有 `lightllm/` 这一棵树是运行时代码，其余顶层目录都是辅助」。如果环境里没有装 setuptools，则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`unit_tests/` 下有 `common/`、`models/`、`server/`、`utils/` 四个子目录，这暗示了什么？

> **答案**：暗示 `lightllm` 包内部也是按 `common / models / server / utils` 这几大职责来划分的——测试目录的组织往往镜像代码目录的组织。这也是一种「通过测试反推代码结构」的阅读技巧。

**练习 2**：为什么 `docs/` 不需要被打进发布的包里？

> **答案**：文档是给人看的（运行时不 `import`），不参与推理。`find_packages` 把它排除，可以让安装包更小。只有运行时真正要用到的非 `.py` 文件（如 `setup.py:3` 里的 kernel 配置 JSON）才需要用 `package_data` 显式声明。

---

### 4.2 lightllm 包的内部组织

#### 4.2.1 概念说明

进入 `lightllm/` 这棵真正的运行时代码树后，会看到它按「功能域」分成了五个顶层子模块：

| 子模块 | 职责（一句话） |
| --- | --- |
| `server/` | **服务层**：HTTP 接口、多进程编排、调度、反 token 化、监控。一切「围绕一次推理请求的服务流程」都在这里。 |
| `common/` | **公共推理层**：与具体模型无关的推理框架——模型基类、层模板、权重加载、KV 内存管理、注意力后端、triton 算子。 |
| `models/` | **模型实现层**：每一个具体模型族（llama、qwen3、deepseek2…）一个目录，复用 `common/` 的框架。 |
| `distributed/` | **分布式通信层**：NCCL 封装、集合通信（all-reduce）。 |
| `utils/` | **工具层**：设备判断、共享内存、日志、配置加载等零散工具函数。 |

这个划分的关键思想是 **「服务流程」与「模型推理」解耦**：

- `server/` 只管「怎么接收请求、怎么调度、怎么把 token 变回文字送出去」，**完全不关心**是哪个模型。
- `common/` + `models/` 只管「怎么把 token 跑成 logits」，**完全不关心**请求是从 HTTP 还是别的地方来的。
- 新增一个模型，你只需要在 `models/` 下加一个目录，`server/` 几乎不用动。这就是 u1-l1 说的「易扩展」的代码层面体现。

#### 4.2.2 核心流程

当一条推理请求进来，代码在这些子模块间的流转大致是：

```
HTTP 请求
  └─ server/        （httpserver 接收 → router 调度 → model_infer 调用后端）
       └─ common/   （basemodel 推理框架 + kv 内存管理 + 注意力算子）
            └─ models/<某模型>/   （具体的层推理、权重、算子）
       └─ distributed/  （TP 时的 all-reduce 通信）
  └─ server/detokenization/  （token → 文本，回流给 httpserver）
```

可以看到：

- **纵向**（`server → common → models`）是「从服务到具体模型」的调用链。
- **横向**（`distributed/`、`utils/`）是被各层共用的基础设施。

这种「服务层 / 公共推理层 / 模型层」的三段式，是后面所有讲义的基础地图。

#### 4.2.3 源码精读

先看包入口。`lightllm/__init__.py` 整个文件只有几行：

```python
from lightllm.utils.device_utils import is_musa

if is_musa():
    import torchada  # noqa: F401
```

见 [lightllm/__init__.py:1-4](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/__init__.py#L1-L4)。它几乎没有内容，只做了一件事：**如果是摩尔线程（MUSA）GPU，就额外 `import torchada`**。这说明 LightLLM 的包入口刻意保持「干净」，把所有逻辑都下沉到子包，入口只做必要的设备适配——这是一种很常见的包组织风格，避免 `import lightllm` 时触发重型初始化。

下面分别看五个子模块的结构。

**(A) `lightllm/server/` —— 服务层（多进程的「家」）**

LightLLM 的每个进程基本都对应 `server/` 下的一个子目录或一组入口文件：

```
lightllm/server/
├── api_server.py        # 服务主入口（hypercorn 拉起 http 服务）
├── api_start.py         # 按 run_mode 编排并拉起各子进程
├── api_cli.py           # 命令行参数解析（argparse）
├── api_http.py          # HTTP 路由：/generate、/v1/chat/completions 等
├── api_openai.py        # OpenAI 兼容 API 适配
├── api_anthropic.py     # Anthropic 兼容 API 适配
├── api_lightllm.py / api_tgi.py / api_models.py
├── tokenizer.py / build_prompt.py        # 分词器、chat 模板构建
├── function_call_parser.py               # 函数调用解析
├── reasoning_parser.py                   # 思考链（reasoning）解析
├── multimodal_params.py / pd_io_struct.py / req_id_generator.py
├── httpserver/          # ★ HTTP 服务进程（manager.py 等）
├── router/              # ★ 调度进程（manager.py 调度循环、req_queue/ 队列、model_infer/ 后端调用、dynamic_prompt/ 前缀缓存）
├── detokenization/      # ★ 反 token 化进程（token → 文本，流式推送）
├── metrics/             # ★ 指标采集进程（manager.py / metrics.py）
├── config_server/       # 配置服务 / NCCL tcp store
├── core/objs/           # 跨进程共享数据结构（Req、共享内存 buffer、采样参数）
├── visualserver/        # 视觉多模态进程
├── audioserver/         # 音频多模态进程
├── embed_cache/         # 多模态嵌入缓存（按 MD5 命中，避免重算）
├── health_monitor/      # 健康检查进程
├── multi_level_kv_cache/ # GPU→CPU→磁盘 多级 KV 缓存
└── httpserver_for_pd_master/ # PD master 的 http
```

记忆技巧：`server/` 下的子目录 ≈ LightLLM 的进程清单。`httpserver / router / detokenization / metrics` 是四个「常驻核心进程」；`visualserver / audioserver / embed_cache / multi_level_kv_cache / health_monitor` 是「可选/扩展进程」。这一点会和 u1-l5（多进程编排）直接对应。

> 提示：`server/router/` 自己还有更深的子目录（`req_queue/`、`model_infer/`、`dynamic_prompt/`），它们是第二单元「请求链路」的重点，本讲只需知道「调度相关的代码都在 `router/` 下」即可。

**(B) `lightllm/common/` —— 公共推理层（模型无关的框架）**

```
lightllm/common/
├── req_manager.py / build_utils.py / infer_utils.py / kernel_config.py / cuda_wrapper.py
├── basemodel/           # ★ 推理框架核心
│   ├── basemodel.py / batch_objs.py / infer_struct.py   # 模型基类、batch 结构、推理状态
│   ├── cuda_graph.py / prefill_cuda_graph.py            # CUDA Graph 捕获/重放
│   ├── multimodal_tokenizer.py
│   ├── attention/         # 注意力后端抽象（fa3/flashinfer/triton…）
│   ├── attention_vit/     # ViT 视觉注意力
│   ├── layer_infer/       # 推理层模板（pre/transformer/post）
│   ├── layer_weights/     # 权重基类 + HF 权重加载
│   └── triton_kernel/     # 公共 triton 算子（采样、惩罚、fused_moe…）
├── kv_cache_mem_manager/ # ★ KV Cache 内存管理（allocator 分配器、fp8/int8 量化变体）
├── quantization/         # 权重量化方法（awq / w8a8 / no_quant…）
├── kv_trans_kernel/      # KV 传输算子（PD 分离用，nccl/nixl）
├── cpu_cache/            # CPU 缓存创建
├── linear_att_cache_manager/ # 线性注意力缓存
├── all_kernel_configs/   # kernel 自动调优配置（JSON，随包分发）
└── triton_utils/         # triton 工具
```

`common/` 是整个项目「技术含量最高」的地方之一，但它的组织逻辑很清晰：**与具体模型无关的东西都放这里**。其中 `basemodel/` 是后续第三单元（推理内核）的主战场，`kv_cache_mem_manager/` 是第四单元（KV 缓存）的主战场。

**(C) `lightllm/models/` —— 模型实现层（一模型一目录）**

`models/` 下目前有约 45 个模型族目录（llama、qwen3、deepseek2、mixtral、gemma3、glm4_moe_lite、whisper、vit、qwen2_5_vl……），外加一个 `registry.py`（模型注册）和 `__init__.py`。**每个模型目录内部结构高度一致**，以 `models/llama/` 为例：

```
lightllm/models/llama/
├── model.py                              # 模型主类（继承 common 的基类，组装各组件）
├── infer_struct.py                       # 该模型的推理状态结构
├── layer_infer/
│   ├── pre_layer_infer.py                # embedding 前处理层推理
│   ├── post_layer_infer.py               # logits 后处理层推理
│   └── transformer_layer_infer.py        # transformer 层推理
├── layer_weights/
│   ├── pre_and_post_layer_weight.py      # embedding/logits 权重
│   └── transformer_layer_weight.py       # transformer 层权重（含 TP 切分）
├── triton_kernel/                        # 该模型专属算子（rotary_emb、silu_and_mul…）
└── yarn_rotary_utils.py                  # YaRN 旋转位置编码工具
```

这种「`model.py` + `layer_infer/` + `layer_weights/` + `triton_kernel/`」的三件套结构是 LightLLM 适配新模型的标准范式，第五单元（模型适配实践）会专门讲它。**现在你只需要记住：看到一个模型名，就去 `models/<模型名>/` 找它的实现。**

**(D) `lightllm/distributed/` 和 `lightllm/utils/`**

- `distributed/`：文件不多但很关键，包含 `pynccl.py`、`pynccl_wrapper.py`（NCCL 封装）、`communication_op.py`（集合通信）、`flashinfer_all_reduce.py`、`symm_mem_all_reduce.py`（两种 all-reduce 实现）。张量并行（TP）下的通信都在这里。
- `utils/`：一堆零散但重要的工具模块，比如 `device_utils.py`（判断 CUDA/MUSA 设备）、`shm_utils.py`（共享内存）、`dist_utils.py`（分布式初始化）、`log_utils.py`（日志）、`health_check.py`、`profile_max_tokens.py` 等。它们被几乎所有其他模块调用。

#### 4.2.4 代码实践

> 这是本讲的核心实践任务。

**实践目标**：亲手画出 `lightllm/server` 与 `lightllm/common` 的二级目录树，并标注每个子目录的职责——产出一张属于你自己的「代码地图」。

**操作步骤**：

1. 在仓库根目录执行 `ls -F lightllm/server/` 和 `ls -F lightllm/common/`，把目录名抄下来。
2. 按下面的模板，画两棵树（只画到二级，即子目录这一层即可，文件可挑关键的列）：

   ```
   lightllm/server/
   ├── httpserver/        # 职责：____________
   ├── router/            # 职责：____________
   ├── detokenization/    # 职责：____________
   ├── metrics/           # 职责：____________
   └── ...（每个子目录都填）
   ```

3. 对照本讲 4.2.3 里给出的职责说明，给每个子目录写一句话职责。
4. （进阶）在 `router/`、`common/basemodel/` 这种「关键大目录」上，再多展开一级（三级目录），标出 `req_queue/`、`model_infer/`、`layer_infer/`、`layer_weights/`、`attention/` 的职责。

**需要观察的现象**：

- `server/` 下的子目录数量明显多于 `common/`，且大多是「进程级」的（每个对应一个进程）。
- `common/` 下的子目录更偏「能力域」（内存管理、量化、注意力、权重），和进程无关。
- `models/` 下每个模型目录长得几乎一样。

**预期结果**：得到两棵带职责标注的目录树。把它存成笔记——后面每一讲读源码时，你都可以在这张图上「点亮」对应的目录。如果你无法在本地执行 `ls`（比如只在网页看代码），可以用 GitHub 仓库页面的文件浏览器代替，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果我想修改「请求的调度策略」，应该去哪个目录？如果想修改「某个模型的注意力计算」，又该去哪？

> **答案**：调度策略属于服务流程 → `lightllm/server/router/`（尤其是 `req_queue/`）。某个模型的注意力计算属于模型推理 → 先看该模型 `lightllm/models/<模型名>/layer_infer/`，若涉及通用注意力后端，则看 `lightllm/common/basemodel/attention/`。

**练习 2**：`lightllm/__init__.py` 为什么这么短？把大量初始化写在这里会有什么坏处？

> **答案**：因为只要 `import lightllm`（哪怕只是用到某个工具函数），`__init__.py` 就会执行。如果把重型初始化（加载 torch、建通信域）写在这里，会拖慢所有 import、甚至引发循环依赖。LightLLM 选择保持入口干净，把逻辑下沉到子包，按需加载（见 [lightllm/__init__.py:1-4](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/__init__.py#L1-L4)）。

**练习 3**：`common/basemodel/` 和 `models/llama/` 都有 `layer_infer/` 和 `layer_weights/`，它们是什么关系？

> **答案**：`common/basemodel/` 里的是**模板/基类**（与模型无关的通用实现），`models/llama/` 里的是**具体实现**（继承/复用模板，针对 Llama 的细节覆写）。通用逻辑在 common，模型差异在 models——这就是「易扩展」的结构基础。

## 5. 综合实践

把本讲内容串起来，做一个「功能 → 目录」的定位练习。下面列出 8 个常见的需求，请你**不看本讲正文**，逐个写出它对应的源码目录（精确到二级或三级），并说明理由：

| 编号 | 需求 | 你定位到的目录 | 理由 |
| --- | --- | --- | --- |
| 1 | 调度循环 `_step` 在哪 | | |
| 2 | 新增一个 OpenAI 兼容的路由 | | |
| 3 | KV Cache 的内存分配/回收 | | |
| 4 | Llama 模型的旋转位置编码算子 | | |
| 5 | TP 下的 all-reduce 通信 | | |
| 6 | 把生成 token 解码成文字 | | |
| 7 | 判断当前是不是 MUSA 设备 | | |
| 8 | 给视觉模型缓存图像嵌入 | | |

完成后，对照下面的参考答案（先自己写再看）：

1. `lightllm/server/router/manager.py`（调度在 router 进程）
2. `lightllm/server/api_openai.py`（HTTP 路由适配在 server 顶层）
3. `lightllm/common/kv_cache_mem_manager/`（KV 内存管理是公共能力）
4. `lightllm/models/llama/triton_kernel/rotary_emb.py`（模型专属算子在 models 下）
5. `lightllm/distributed/`（集合通信在 distributed）
6. `lightllm/server/detokenization/`（反 token 化是独立进程）
7. `lightllm/utils/device_utils.py`（设备判断是通用工具，且 `__init__.py` 就引用了它）
8. `lightllm/server/embed_cache/`（多模态嵌入缓存是 server 下的扩展进程）

**目标**：如果你能答对 6 个以上，说明你已经在脑子里建好了 LightLLM 的代码地图，可以放心进入第二单元读真正的调用链了。

## 6. 本讲小结

- 仓库顶层目录分为「**运行时主包 `lightllm/`**」和「**非运行时辅助**（`docs/`、`test/`、`unit_tests/`、`demos/`、`docker/`、`tools/`）」两大区，`setup.py` 的 `find_packages` 排除列表官方确认了这一点。
- `lightllm` 包内部按五大职责划分：`server/`（服务流程）、`common/`（公共推理框架）、`models/`（具体模型实现）、`distributed/`（通信）、`utils/`（工具）。
- **服务层与模型层解耦**：`server/` 只管请求流转，不关心模型；`common/` + `models/` 只管推理，不关心请求来源。新增模型只需动 `models/`。
- `server/` 的子目录 ≈ 进程清单（httpserver / router / detokenization / metrics 是核心，visualserver / embed_cache / multi_level_kv_cache 等是扩展）。
- `models/` 下每个模型目录结构高度一致（`model.py` + `layer_infer/` + `layer_weights/` + `triton_kernel/`），是适配新模型的标准范式。
- `lightllm/__init__.py` 刻意保持极简，只做必要的设备适配，避免重型初始化拖慢 import。

## 7. 下一步学习建议

有了代码地图，下一步就该**沿着一次真实请求走一遍代码**了。建议：

- 下一讲 **u1-l4（服务启动入口与命令行参数）**：从 `lightllm/server/api_server.py` 这个入口文件出发，看服务到底是怎么起来的、有哪些命令行参数。
- 之后 **u1-l5（多进程编排启动流程）**：读 `api_start.py`，看本讲列出的那一堆 `server/` 子目录里的进程，是如何被依次拉起的——本讲的「进程清单」会在那一讲变成真实的启动顺序。
- 如果你想立刻验证本讲的地图，可以挑一个 `models/<某模型>/model.py` 打开，对照 `common/basemodel/basemodel.py`，感受「具体实现继承公共框架」的关系，为第三单元（推理内核）做铺垫。
