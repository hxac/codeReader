# 代码地图：仓库目录结构与模块导览

## 1. 本讲目标

上一讲（u1-l1）我们回答了「DeepSpeed 是什么、为什么需要它」；本讲回答「它的代码长什么样、住在哪里」。学完本讲，你应该能够：

1. 画出仓库顶层的目录地图，说出 `deepspeed/`、`op_builder/`、`csrc/`、`bin/`、`tests/`、`docs/` 各自承担的角色。
2. 画出 `deepspeed/` 主包下 20+ 个子模块的职责地图，并能区分四层分工：**runtime（训练运行时）→ ops（算子的 Python 前端）→ op_builder（算子编译器）→ csrc（CUDA/C++ 内核源码）**。
3. 通过精读 [deepspeed/__init__.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py) 的导出内容，说出用户可见的公共 API 面是什么，并理解「懒加载」与「import 不能初始化 CUDA」两条工程约束。
4. 识别本周期（0.20.x）新增的 `deepspeed/models/` **模型族包**目录：一个模型家族一个包（首个家族 `glm_moe_dsa`，对应 GLM-5.2），`import deepspeed` 不导入它，家族的可选依赖（如 TileLang）只由该家族的用户承担。
5. 拿着这张地图，知道本手册后续每个单元会深入哪个目录——从此阅读任何 DeepSpeed 报错栈时都能立刻定位「我在哪一层」。

本讲不写训练代码，产出物是一份你自己标注的目录地图，它是后续所有讲义的坐标系。

## 2. 前置知识

承接 u1-l1 的两个术语：**参数量 Ψ**（模型参数总数）与**数据并行的显存冗余**（每张卡都存完整模型副本）。ZeRO 通过切分优化器状态/梯度/参数来消除冗余——这些机制的实现代码就住在 `deepspeed/runtime/zero/` 里，本讲先认路，单元 5 再进门。

本讲还需要三个 Python 工程基础概念，用大白话解释：

1. **包（package）与 `__init__.py`**：Python 里一个带 `__init__.py` 的目录就是一个「包」。当用户执行 `import deepspeed` 时，解释器执行的就是 `deepspeed/__init__.py` 这个文件。因此这个文件里的 `import` 与 `from ... import ...` 语句，**就等于把哪些名字暴露给用户**——它定义了「公共 API 面」。`from .runtime.engine import DeepSpeedEngine` 执行后，用户就能写 `deepspeed.DeepSpeedEngine`。
2. **符号链接（symlink）**：文件系统里的「快捷方式」。本仓库有 3 处关键 symlink（`deepspeed/accelerator`、`deepspeed/ops/op_builder`、`deepspeed/ops/csrc`），它们让**同一份源码**既能被安装脚本在「安装期」导入，又能被用户在「运行期」从包内路径导入——这是理解仓库布局的钥匙，4.4 节详解。
3. **JIT 编译（Just-In-Time）**：pip 安装时不编译 CUDA 算子，等运行时第一次用到某个算子才现场编译并缓存。与之相对的是「安装期预编译」（`DS_BUILD_OPS=1`）。两种模式共用同一套 builder 代码，即 `op_builder/` 目录——u1-l3 会展开，本讲只认识它的位置。

另外两个小概念：**nccl/gloo** 是 PyTorch 分布式通信的后端实现（卡间传数据的底层库）；**nvcc** 是 NVIDIA 的 CUDA 编译器。

## 3. 本讲源码地图

本讲精读/引用的关键文件：

| 文件 | 作用 | 在本讲中的角色 |
|---|---|---|
| [deepspeed/__init__.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py) | 包入口，定义公共 API（`initialize`、`init_inference` 等） | 4.3 节主角：公共 API 面与懒加载 |
| [deepspeed/constants.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/constants.py) | 全局常量（默认端口、超时） | 展示「常量收敛到一处」的组织方式 |
| [deepspeed/git_version_info.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/git_version_info.py) | 版本信息装载（安装时生成） | 打通 u1-l1 讲的版本传递链 |
| [setup.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/setup.py) | 安装脚本 | 解释顶层 `op_builder/` 为何存在 |
| [deepspeed/pipe/__init__.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/pipe/__init__.py) | 顶层 `pipe` 包的再导出 | 展示「门面包 → 实现包」模式 |
| [deepspeed/models/__init__.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/__init__.py) | 模型族包的约定说明（本周期新增） | 4.5 节主角：一族模型一套训练定制 |
| [CONTRIBUTING.md](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/CONTRIBUTING.md) | 贡献指南 | 串起 tests/ 目录与贡献流程 |

此外本讲大量「测绘」以下目录（用 `ls` 逐一验证过）：仓库根目录、`deepspeed/` 全部 28 个子项（23 个子目录 + 5 个 `.py` 文件）、`deepspeed/runtime/`、`deepspeed/ops/`、`deepspeed/models/`、`csrc/`、`op_builder/`、`bin/`、`tests/`。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：**4.1 仓库顶层总地图**、**4.2 runtime 目录与主包子模块地图**、**4.3 包导出与懒加载**、**4.4 ops 与 csrc 目录**、**4.5 deepspeed/models 模型族包**。

### 4.1 仓库顶层总地图

#### 4.1.1 概念说明

DeepSpeed 不是单一的 `.py` 库，而是「**Python 包 + CUDA/C++ 内核源码 + 安装期编译器 + CLI 工具 + 测试矩阵**」的复合仓库。阅读它的第一步是把顶层目录按职能分成四类，避免在 30+ 个顶层条目里迷路。分类的标准是问一个问题：**「这个目录里的代码，在什么时刻被谁使用？」**

- **运行期核心**：用户 `import deepspeed` 后真正执行的 Python 代码（`deepspeed/`）。
- **安装期工具**：只在 `pip install` 那一刻被 setup.py 使用（`op_builder/`、顶层 `accelerator/`）。
- **内核源码**：不直接执行，需编译成 `.so` 后被 Python 调用（`csrc/`）。
- **工程设施**：CLI 入口（`bin/`）、文档（`docs/`、`blogs/`）、测试（`tests/`）、示例（`examples/`）、CI/发布（`ci/`、`release/`、`scripts/`）等。

#### 4.1.2 核心流程

以「用户执行 `pip install deepspeed` → `import deepspeed` → `deepspeed --num_gpus 2 train.py`」这条生命周期为线索，顶层目录的参与顺序是：

```
pip install
  └─ setup.py 被执行
       ├─ import 顶层 op_builder/     （读取算子注册表，决定编译什么）
       └─ 读取 version.txt            （版本号源头，见 u1-l1）
            │
            ▼
import deepspeed
  └─ 执行 deepspeed/__init__.py       （加载运行期核心；用到算子时经
                                        deepspeed/ops/op_builder 触发 JIT，
                                        编译 csrc/ 下的内核并缓存）
            │
            ▼
deepspeed --num_gpus 2 train.py
  └─ 执行 bin/deepspeed 脚本          （拉起多进程，最终 import deepspeed）
       └─ 训练过程可被 tests/ 中的用例复现、被 docs/ 文档描述
```

记住这条「安装 → 导入 → 启动」时间线，下面每个目录都能挂到时间线的一个刻度上。

#### 4.1.3 源码精读

**（1）顶层目录全景**（在仓库根目录执行 `ls` 的实际结果，按职能重排）：

```text
# 运行期核心（本手册主战场）
deepspeed/          # Python 主包：引擎、ZeRO、并行、推理……单元 2~9 几乎都在这里

# 安装期工具（setup.py 才导入；通过 symlink 同时服务运行期，见 4.4）
op_builder/         # 算子构建器：每个 CUDA 算子一个 builder 类
accelerator/        # 加速器抽象（CUDA/CPU/HPU/XPU/NPU...，单元 3 详讲）

# 内核源码（C++/CUDA，编译成 .so 后使用）
csrc/               # adam、aio、transformer 等 19 个内核子目录

# 工程设施
bin/                # CLI：deepspeed、ds_report、ds_bench、ds_ssh 等 11 个脚本
tests/              # unit / model / perf / accelerator / hybrid_engine 等
docs/               # Sphinx 文档源码；_pages/ 下是 training.md、config-json.md 等
examples/           # 官方示例
benchmarks/         # 性能基准
blogs/              # 技术博客（README 里 Latest News 的落地文）
requirements/       # 依赖清单（按场景拆分多个 txt）
ci/  scripts/  release/  docker/  azure/   # CI 配置、辅助脚本、发布与容器
setup.py  install.sh  Makefile  version.txt  CONTRIBUTING.md  AGENTS.md ...
```

**（2）setup.py 为什么「安装期」就要导入顶层目录**——这是顶层布局存在的根本原因。[setup.py:40-41](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/setup.py#L40-L41)：

```python
from op_builder import get_default_compute_capabilities, OpBuilder
from op_builder.all_ops import ALL_OPS, accelerator_name
```

这两行说明：安装脚本执行时 `deepspeed` 包尚未就位，必须从**顶层**导入 `op_builder`。这正是 `op_builder/`、`accelerator/` 不放进 `deepspeed/` 里面的原因——**安装期与运行期是两个不同的导入环境**。u1-l3 会展开这条编译链。

**（3）tests/ 与 docs/ 的内部结构**（`ls tests/`、`ls docs/_pages/` 验证）：

```text
tests/
  unit/                  # 单元测试大本营（simple_model.py 测试基建在单元 2 反复使用）
  model/                 # 真实模型回归测试
  perf/  benchmarks/     # 性能与基准
  accelerator/           # 各加速器后端测试
  hybrid_engine/  lightning/  torch_compile/  small_model_debugging/
docs/_pages/
  training.md  inference.md  config-json.md   # 三份核心文档，单元 2 会精读 config-json
```

贡献流程写在 [CONTRIBUTING.md](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/CONTRIBUTING.md) 里：测试章节从 [CONTRIBUTING.md:29](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/CONTRIBUTING.md#L29) 开始（单元测试在 L36 起），新功能需要「提案先行」的三步流程从 [CONTRIBUTING.md:131](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/CONTRIBUTING.md#L131) 开始。u9-l4 会专门讲测试体系。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证顶层目录的分类，并确认「安装期目录被 setup.py 引用」这一事实。
2. **操作步骤**：
   - 在仓库根目录执行 `ls`，对照上面 4.1.3 的全景图逐个核对；
   - 执行 `grep -n "op_builder\|accelerator" setup.py`，观察哪些顶层目录名出现在安装脚本里。
3. **需要观察的现象**：`ls` 输出的目录都能归入四类之一；`grep` 结果中 `setup.py` 第 40-41 行导入了 `op_builder`。
4. **预期结果**：约 17 个顶层目录 + 十余个顶层文件；`grep` 至少命中 `op_builder` 的导入行。若你的环境中 `grep` 无输出，检查是否在仓库根目录执行。
5. 本实践为纯阅读型，不依赖 GPU，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：`blogs/` 和 `docs/` 都是 Markdown 文档，为什么分成两个目录？
**答案**：`docs/` 是随 readthedocs 构建的用户手册（有 `config-json.md`、`training.md` 这类参考文档）；`blogs/` 是技术博客（如 ZeRO-Infinity 论文解读），内容偏「为什么这样设计」。前者是查手册，后者是读故事。

**练习 2**：如果删掉 `bin/` 目录，`pip install deepspeed` 装出来的库还能 `import deepspeed` 吗？还能用 `deepspeed` 命令启动训练吗？
**答案**：能 import——`bin/` 里的脚本只是 CLI 入口，不属于 Python 包本体；但不能再用 `deepspeed` 命令（该命令由 `bin/deepspeed` 脚本提供，u1-l4 详讲）。不过仍可用 `torchrun` 等方式启动，因为 CLI 只是多种启动方式之一。

**练习 3**：顶层 `accelerator/` 与 `deepspeed/accelerator/` 内容几乎相同，是两份代码吗？
**答案**：不是。`deepspeed/accelerator` 是指向 `../accelerator/` 的**符号链接**（symlink），真实代码只有顶层一份。`op_builder`、`csrc` 同理，见 4.4 节。

### 4.2 deepspeed/ 主包与 runtime 目录：训练运行时的心脏

#### 4.2.1 概念说明

`deepspeed/` 主包是用户 `import` 的对象，内含 28 个子项（23 个子目录 + 5 个 `.py` 文件；其中 `models/` 是 0.20.x 周期新增的目录，见 4.5 节）。它们不是平铺的杂物间，而是按「子系统」划分的房间。其中最重要的是 **`runtime/`（运行时）**：训练引擎、配置系统、ZeRO、流水线、混合精度优化器全部住在这里——本手册单元 2、4、5、8 的大多数讲义都在 `runtime/` 的子树里。

理解主包的关键是抓住一个主干与多条支线：**主干 = `runtime/engine.py`（引擎）+ `runtime/config.py`（配置）**，一切功能都从引擎构造函数中被装配；支线 = 各并行/优化子系统目录，被引擎按配置**按需启用**。

#### 4.2.2 核心流程

`deepspeed/` 子模块按「离引擎的距离」分层，调用关系自上而下：

```text
deepspeed/__init__.py          # 门面：initialize() 按配置分发引擎
        │
        ▼
deepspeed/runtime/             # ★ 训练运行时（主干）
   ├─ engine.py                #   DeepSpeedEngine：单元 2 的主角
   ├─ config.py                #   DeepSpeedConfig：JSON 配置解析
   ├─ zero/                    #   ZeRO 全家（单元 5，8 篇讲义）
   │    ├─ stage_1_and_2.py  stage3.py  partition_parameters.py
   │    └─ parameter_offload.py  parameter_offload 协调器等
   ├─ pipe/                    #   流水线并行（单元 6）
   ├─ fp16/  bf16_optimizer.py #   混合精度优化器（单元 4）
   ├─ swap_tensor/             #   ZeRO-Infinity 的 GPU↔CPU↔NVMe 交换（单元 5）
   ├─ checkpoint_engine/       #   可插拔检查点引擎（单元 8）
   ├─ activation_checkpointing/#   激活检查点（单元 8）
   ├─ tensor_parallel/  sequence_parallel/  data_pipeline/
   ├─ hybrid_engine.py  rollout/  # 训推一体（单元 7）
   └─ zenflow/  superoffload/  domino/     # 前沿 offload 变体（单元 9）
        │
        ▼  供引擎调用的支线子系统
deepspeed/comm/                # 分布式通信统一入口（单元 3）
deepspeed/accelerator/         # 硬件抽象层（symlink → 顶层，单元 3）
deepspeed/launcher/            # 多进程/多节点启动（单元 1/3）
deepspeed/moe/  sequence/      # 专家并行、序列并行（单元 6）
deepspeed/module_inject/       # AutoTP/AutoEP 模型注入（单元 6/7）
deepspeed/models/              # 模型族包：一族模型一套训练定制（新增，4.5 节/u9-l6）
deepspeed/inference/           # 推理引擎 v1/v2（单元 7）
deepspeed/ops/                 # 算子 Python 前端（4.4 节）
deepspeed/checkpoint/          # Universal Checkpoint（单元 8）
deepspeed/compile/             # DeepCompile（单元 8）
deepspeed/monitor/  profiling/ # 监控与 FLOPs 剖析（单元 8）
deepspeed/autotuning/          # 自动调优（单元 8）
deepspeed/nvme/                # NVMe 异步 IO 与测速工具（单元 5）
deepspeed/utils/               # 工具集：groups.py 进程组、zero_to_fp32.py 导出
deepspeed/linear/  compression/  io/  datastates/  model_implementations/  pipe/(再导出)
```

一个值得注意的细节：`deepspeed/pipe/` 只是个**再导出门面**——[deepspeed/pipe/__init__.py:6-7](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/pipe/__init__.py#L6-L7)：

```python
from ..runtime.pipe import PipelineModule, LayerSpec, TiedLayerSpec
from ..runtime.pipe.dualpipev import DualPipeVModule
```

这 3 行说明：顶层 `pipe` 包只是把 `runtime/pipe` 的实现换个门牌，方便用户写 `from deepspeed.pipe import PipelineModule`。**看到「门面包」，就去 runtime 找实现**——这是本仓库的常见模式。

#### 4.2.3 源码精读

**（1）`deepspeed/runtime/` 的完整清单**（`ls deepspeed/runtime/` 验证，条目后是本手册映射）：

| 条目 | 职责 | 后续单元 |
|---|---|---|
| `engine.py` | DeepSpeedEngine 训练引擎 | u2-l3/l4 |
| `config.py` / `config_utils.py` | DeepSpeedConfig 配置解析与校验 | u2-l2 |
| `zero/` | ZeRO 1/2/3、参数切分与卸载 | u5（8 篇） |
| `pipe/` | PipelineModule、调度器、拓扑 | u6-l3/l4 |
| `fp16/` | loss_scaler、fused/unfused 优化器 | u4-l1/l2 |
| `bf16_optimizer.py` / `precision_config.py` | bf16 与精度配置 | u4-l3 |
| `swap_tensor/` | ZeRO-Infinity 张量交换 | u5-l7 |
| `checkpoint_engine/` | torch/fast/decoupled 检查点引擎 | u8-l2 |
| `model_checkpointing/` | save/load_checkpoint 实现 | u5-l8 |
| `activation_checkpointing/` | 激活重计算与卸载 | u8-l1 |
| `hybrid_engine.py` / `rollout/` / `hybrid_engine_graph.py` | 训推一体引擎 | u7-l4 |
| `zenflow/` / `superoffload/` | 新一代 offload 调度 | u9-l3 |
| `tensor_parallel/` / `sequence_parallel/` / `data_pipeline/` | 各维并行支撑 | u6 |
| `dataloader.py` / `base_optimizer.py` / `lr_schedules.py` | 数据、优化器基类、LR 调度 | u2/u4-l4 |
| `compiler.py` | DeepCompile 运行时挂钩 | u8-l4 |
| `domino/` 等 | 实验性子系统 | — |

**（2）`deepspeed/runtime/zero/` 内部**（`ls` 验证）：`stage_1_and_2.py`（ZeRO-1/2 优化器）、`stage3.py`（ZeRO-3 优化器）、`partition_parameters.py`（`zero.Init` 参数切分）、`parameter_offload.py` + `partitioned_param_coordinator.py`（参数按需物化）、`config.py`（ZeRO 配置类）、`contiguous_memory_allocator.py` 等——单元 5 将逐个精读，这里先记住「文件名 = 功能名」的命名规律，DeepSpeed 源码命名相当直白。

**（3）常量收敛一处**：[deepspeed/constants.py:12-19](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/constants.py#L12-L19)：

```python
TORCH_DISTRIBUTED_DEFAULT_PORT = 29500
default_pg_timeout = timedelta(minutes=int(os.getenv("DEEPSPEED_TIMEOUT", default=10)))
```

第 12 行定义了分布式训练默认端口 29500（4.3 节会看到 `initialize()` 的默认参数引用它）；第 19 行定义进程组默认超时 10 分钟，可用环境变量 `DEEPSPEED_TIMEOUT` 覆盖——这是「魔法数字集中管理」的范例，排查多机卡死问题时你会回来查这一行。

#### 4.2.4 代码实践

1. **实践目标**：不看讲义，独立测绘 `deepspeed/runtime/`，并按「我认识/我不认识」给文件分级，建立自己的阅读优先级。
2. **操作步骤**：
   - 执行 `ls deepspeed/runtime/`；
   - 对每个条目写一行职责猜测（猜不出就标「?」）；
   - 用 `ls deepspeed/runtime/zero/` 与 `ls deepspeed/runtime/pipe/` 再看两层。
3. **需要观察的现象**：`zero/` 下的 `stage_1_and_2.py` 与 `stage3.py` 文件名是否与 ZeRO 阶段对应；`pipe/` 下是否有 `schedule.py`（调度）与 `topology.py`（拓扑）。
4. **预期结果**：能对 `runtime/` 下 30+ 个条目中的至少一半写出合理职责；「?」条目记下来，它们就是你在单元 2-8 中要逐个消灭的目标。
5. 本实践为源码阅读型，无需运行环境。

#### 4.2.5 小练习与答案

**练习 1**：ZeRO-3 的优化器主逻辑在哪个文件？参数切分（`zero.Init`）又在哪个文件？
**答案**：优化器主逻辑在 `deepspeed/runtime/zero/stage3.py`（类 `DeepSpeedZeroOptimizer_Stage3`）；参数切分在 `deepspeed/runtime/zero/partition_parameters.py`。两个关注点分开成两个文件，说明「优化器状态管理」与「参数布局」是正交的两件事——u5-l2 与 u5-l6 分别精读。

**练习 2**：`fp16/` 是个子目录（含 `loss_scaler.py` 等三个文件），而 `bf16_optimizer.py` 却是 `runtime/` 下的单文件。这种不对称说明什么？
**答案**：fp16 路径更复杂——需要独立的 loss scaler（溢出检测）、fused 与 unfused 两种优化器包装，所以拆成子目录；bf16 不需要 loss scaling（u4-l3 讲数值原因），一个文件装得下。目录结构的粒度往往暗示问题本身的复杂度。

**练习 3**：`deepspeed/utils/groups.py` 管什么？为什么它不在 `runtime/` 里？
**答案**：它管理各并行维度（data/tensor/pipeline/expert）的进程组划分与缓存（u3-l3 精读）。放在 `utils/` 是因为它是被 comm、runtime、moe 等多个子系统共用的底层工具，不专属于训练运行时。

### 4.3 包导出与懒加载：deepspeed/__init__.py 精读

#### 4.3.1 概念说明

[deepspeed/__init__.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py) 共 818 行，是全仓库被「import 得最多」的文件。它回答一个关键问题：**用户可见的公共 API 面到底有多大？** 答案出人意料地小——核心只有两个函数（`initialize`、`init_inference`）加十几个类。其余 700 多行是辅助逻辑。

「懒加载（lazy loading）」指：**能推迟的初始化一律推迟到第一次真正使用时**。这个文件里有两个教科书级案例：延迟计算 `HAS_TRITON`（省导入时间）、延迟绑定 `dist`（省初始化成本）。AGENTS.md 中「import deepspeed 不得初始化 CUDA、重导入不得超 0.1 秒」的硬性规定，就是靠这些细节保障的。

#### 4.3.2 核心流程

`import deepspeed` 时的执行顺序：

```text
1. 设置 PYTORCH_NVML_BASED_CUDA_CHECK=1     ← 保证探测 CUDA 不创建上下文
2. 顺序执行模块级 import                     ← 把引擎/配置等类装入命名空间 = 公共 API 面
3. 装配版本信息 __version__ / __git_hash__   ← 来自 git_version_info.py
4. dist = None                               ← 分布式句柄先置空，等 initialize() 再绑定
（此后用户访问 deepspeed.HAS_TRITON 才触发 __getattr__ → 现算）
```

用户调用 `deepspeed.initialize(model, config)` 时的分发流程：

```text
initialize()
  ├─ 关闭可能开着的 zero.Init 上下文（防止切分泄漏到引擎构造）
  ├─ dist.init_distributed(...)              ← 此时才真正建立通信
  ├─ 解析 config（字符串路径 → 字典）
  ├─ 分发引擎类：
  │    ├─ 模型是 PipelineModule/DualPipeVModule → PipelineEngine / DualPipeVEngine
  │    ├─ 配置启用 hybrid_engine             → DeepSpeedHybridEngine
  │    └─ 其余                                → DeepSpeedEngine（默认）
  └─ 返回 (engine, engine.optimizer, engine.training_dataloader, engine.lr_scheduler)
```

#### 4.3.3 源码精读

**（1）第一条工程约束：import 不许碰 CUDA 上下文**。[deepspeed/__init__.py:8-14](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L8-L14)：

```python
# By default, PyTorch's CUDA availability check (cudaGetDeviceCount/cuInit)
# creates a CUDA context, which poisons fork()-based multiprocessing once
# DeepSpeed probes op compatibility at import time. Opt into PyTorch's
# NVML-based availability check so importing DeepSpeed never creates a
# CUDA context, before importing torch or anything that may query CUDA.
os.environ.setdefault("PYTORCH_NVML_BASED_CUDA_CHECK", "1")
```

这段代码必须在 `import torch` **之前**执行。原因写在注释里：默认的 CUDA 探测会创建 CUDA 上下文，而一旦创建了上下文，基于 `fork()` 的多进程 DataLoader 就会中毒失效。`setdefault` 而非直接赋值，是为了尊重用户的显式设置。

**（2）懒加载案例一：`HAS_TRITON` 延迟计算**。[deepspeed/__init__.py:35-41](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L35-L41)：

```python
def __getattr__(name):
    # HAS_TRITON is computed on first access because importing triton slows down import deepspeed.
    if name == "HAS_TRITON":
        globals()["HAS_TRITON"] = _has_triton()
        return globals()["HAS_TRITON"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

利用 PEP 562 的模块级 `__getattr__`：只有当有人第一次访问 `deepspeed.HAS_TRITON` 时才导入 triton 并计算结果，随后缓存到全局变量。导入 triton 很慢，这个技巧把成本从「所有人」转移到「真正用到 triton 内核的人」。

**（3）公共 API 面本体**。[deepspeed/__init__.py:44-72](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L44-L72) 这不到 30 行 import 就定义了用户能直接 `deepspeed.XXX` 访问的全部名字，摘录关键几行：

```python
from .runtime.engine import DeepSpeedEngine, ...        # 训练引擎
from .runtime.pipe.engine import PipelineEngine          # 流水线引擎
from .inference.engine import InferenceEngine            # 推理引擎
from .runtime.config import DeepSpeedConfig, DeepSpeedConfigError
from .runtime.activation_checkpointing import checkpointing
from .module_inject import replace_transformer_layer, set_autotp_mode
from .comm.comm import init_distributed
from .runtime import zero
```

对着这批 import 画一条分界线：**`initialize`/`init_inference` 是你日常唯二需要调用的入口函数**，其余类（如 `DeepSpeedConfig`）多数由引擎内部使用。这解释了为什么 DeepSpeed 接入只需三步（u2-l1 详讲）。

**（4）懒加载案例二：`dist = None` 延迟绑定**。[deepspeed/__init__.py:87-88](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L87-L88)：

```python
# Set to torch's distributed package or deepspeed.comm based inside DeepSpeedEngine init
dist = None
```

导入时 `dist` 只是空指针，直到 `initialize()` 内部（[deepspeed/__init__.py:507-512](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L507-L512)）才真正建立分布式：

```python
global dist
from deepspeed import comm as dist
dist_backend = get_accelerator().communication_backend_name()
dist.init_distributed(dist_backend=dist_backend, ...)
```

**（5）`initialize()` 的引擎分发**。[deepspeed/__init__.py:553-597](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L553-L597) 是本文件的「主干道」：先判断模型是否为 `PipelineModule`（是 → 流水线引擎，且 `DualPipeVModule` 对应 `DualPipeVEngine`）；再判断配置是否启用 hybrid_engine（是 → `DeepSpeedHybridEngine`）；默认落到 `DeepSpeedEngine`。注意 [deepspeed/__init__.py:502-503](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L502-L503) 在构造前调用 `zero.partition_parameters.shutdown_init_context()`、[deepspeed/__init__.py:599-600](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L599-L600) 构造后恢复——`zero.Init` 上下文下模型参数生来就是切分的（u5-l2），引擎构造期间要临时收起这套机制。

**（6）版本信息装配**。[deepspeed/__init__.py:81-85](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L81-L85) 把 `git_version_info.py` 装载的值导出为 `__version__` 与 `__git_hash__`；而 [deepspeed/git_version_info.py:8-21](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/git_version_info.py#L8-L21) 展示了双通道：已安装时读 setup.py 生成的 `git_version_info_installed.py`；未安装的源码 checkout 则回退读 `version.txt`（即 u1-l1 讲的 0.20.1，0.20.0 已于本周期发布），git hash 记为 `[none]`。

#### 4.3.4 代码实践

1. **实践目标**：用两个一行命令验证「版本装配」与「HAS_TRITON 懒加载」真实存在。
2. **操作步骤**（在装好 torch 的环境中、仓库根目录下执行）：

   ```bash
   # a) 查看版本装配结果
   python -c "import deepspeed; print(deepspeed.__version__, deepspeed.__git_hash__)"

   # b) 验证 triton 未随 import 提前加载
   python -c "import sys, deepspeed; print('triton loaded:', 'triton' in sys.modules); print('HAS_TRITON:', deepspeed.HAS_TRITON); print('triton loaded now:', 'triton' in sys.modules)"
   ```

3. **需要观察的现象**：命令 a 输出版本号与 git hash；命令 b 第一个 `triton loaded` 应为 `False`，访问 `HAS_TRITON` 之后（若环境有 triton）第三个输出可能变为 `True`。
4. **预期结果**：未 pip install 的源码目录中，命令 a 输出 `0.20.1 [none]`（对应 git_version_info.py 的回退分支）；已 `pip install -e .` 则输出真实短 hash。命令 b 展示懒加载生效的直接证据。
5. 若当前环境无 GPU/torch，命令仍可运行（这正是设计目标），但输出值以实际环境为准，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`initialize()` 返回值是什么？为什么是元组而不是单个引擎对象？
**答案**：返回 `(engine, optimizer, training_dataloader, lr_scheduler)` 四元组（[deepspeed/__init__.py:604-610](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L604-L610)）。因为用户传入的优化器/数据集/调度器都可能被 DeepSpeed 包装替换（例如换成 ZeRO 优化器），返回的是**包装后**的版本，方便用户拿去继续调用。

**练习 2**：为什么 `PYTORCH_NVML_BASED_CUDA_CHECK` 必须用 `setdefault` 而不是直接赋值？
**答案**：`setdefault` 只在用户没有设置该变量时才生效，保留用户显式配置的优先级；直接赋值会覆盖用户意愿。这是库代码尊重宿主环境的通用礼仪。

**练习 3**：`init_inference()`（[deepspeed/__init__.py:671-748](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L671-L748)）与 `initialize()` 的引擎分发有何不同？
**答案**：`init_inference` 不做分发——只有唯一目标 `InferenceEngine`（L746）；它多做的一步是把 `config` 字典与 `kwargs` 合并，且重叠键值不同则报错（L737-L742）。训练入口是「多引擎选一」，推理入口是「配置归一化」。

### 4.4 ops 与 csrc 目录：一个算子的三件套与顶层双轨

#### 4.4.1 概念说明

`deepspeed/ops/`（算子的 **Python 前端**：加载 `.so`、封装成类）、顶层 `csrc/`（**CUDA/C++ 内核源码**）、顶层 `op_builder/`（**编译器**：把 csrc 源码变成 `.so`）构成 DeepSpeed 的算子体系。同一个高性能算子（如 fused Adam）在三处各有一份代码，各司其职：

| 位置 | 语言 | 角色 | 类比 |
|---|---|---|---|
| `csrc/adam/` | CUDA/C++ | 内核实现（真正干活的循环） | 发动机 |
| `op_builder/fused_adam.py` | Python | 编译配方（源文件列表、编译 flags） | 装配线 |
| `deepspeed/ops/adam/fused_adam.py` | Python | 用户接口（加载 `.so`、暴露 `FusedAdam` 类） | 方向盘和仪表盘 |

而「顶层目录 + 包内 symlink」的**双轨布局**解决了两难：安装期 setup.py 需要从顶层导入 builder（此时包未装好）；运行期代码又需要从 `deepspeed.ops.op_builder` 这样的包内路径导入。一份代码、两个名字，靠符号链接打通。

#### 4.4.2 核心流程

以 fused Adam 为例，一个算子从源码到被调用：

```text
安装期（可选预编译，DS_BUILD_OPS=1）
  setup.py → from op_builder.all_ops import ALL_OPS   # 注册表枚举全部算子
          → 逐个调用 builder 的编译方法（nvcc 编译 csrc/adam/*.cu → adam_fused_ops.so）

运行期（默认 JIT，未预编译时）
  import deepspeed → 用户训练用到 FusedAdam
  → deepspeed/ops/op_builder（symlink → 顶层 op_builder）检查 .so 是否已在缓存
  → 未缓存则现场调用 nvcc 编译（同样的 builder 代码）→ 缓存到 ~/.cache/deepspeed
  → deepspeed/ops/adam/fused_adam.py 加载 .so 并包装成 Python 类
```

三个 symlink 的对应关系（`ls -la` 验证）：

```text
deepspeed/accelerator      -> ../accelerator/        （顶层 accelerator/ 是真身）
deepspeed/ops/op_builder   -> ../../op_builder       （顶层 op_builder/ 是真身）
deepspeed/ops/csrc         -> ../../csrc             （顶层 csrc/ 是真身）
```

#### 4.4.3 源码精读

**（1）安装期对顶层的依赖**：[setup.py:40-41](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/setup.py#L40-L41)（前面 4.1.3 已引）从顶层 `op_builder` 导入 `ALL_OPS` 注册表——它枚举了全部可编译算子，u9-l1 精读。

**（2）Windows 下 symlink 失效的补丁**：[setup.py:220-222](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/setup.py#L220-L222)：

```python
shutil.rmtree('.\\deepspeed\\ops\\op_builder', ignore_errors=True)
pathlib.Path('.\\deepspeed\\ops\\op_builder').unlink(missing_ok=True)
shutil.copytree('.\\op_builder', '.\\deepspeed\\ops\\op_builder', dirs_exist_ok=True)
```

这段 Windows 构建专用逻辑：先删掉包内 `op_builder`（Windows 打包不保留 symlink），再把顶层目录**物理拷贝**进去。它是「双轨布局」最直白的证据——symlink 与 copytree 是同一意图在两类操作系统上的两种实现。

**（3）`deepspeed/ops/` 前端清单**（`ls` 验证）：`adam/`（含 `fused_adam.py`、`cpu_adam.py`、`zenflow_cpu_adam.py` 等 6 个文件）、`lamb/`、`lion/`、`adagrad/`、`aio/`（异步 IO）、`transformer/`（推理 transformer 内核）、`quantizer/`、`fp_quantizer/`、`gds/`（GPUDirect Storage）、`pin_memory/`、`triton_ops/`（Triton 内核，无需 nvcc）、`deepspeed4science/` 等。而顶层 `csrc/` 与 `deepspeed/ops/csrc/`（同一目录）下的 `adam/`、`aio/`、`transformer/` 等子目录与之一一呼应——**前端目录名 ≈ 内核目录名**，如 `ops/adam ↔ csrc/adam`。

**（4）Python 前端长什么样**：`deepspeed/ops/adam/` 下既有 GPU 版 `fused_adam.py` 也有 CPU 版 `cpu_adam.py`（ZeRO-Offload 用，u5-l4 精读）和 zenflow 变体（u9-l3），一个优化器算子按部署位置拆成多个文件——单元 4/5 会回来精读其中一个。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证「一个算子、三处代码」与三个 symlink 的存在。
2. **操作步骤**：

   ```bash
   # a) 验证三个 symlink
   ls -la deepspeed/ | grep accelerator          # 应显示 -> ../accelerator/
   ls -la deepspeed/ops/ | grep -E "csrc|op_builder"

   # b) 追踪 adam 算子的三件套
   ls csrc/adam/                                  # CUDA 内核源码
   ls op_builder/fused_adam.py                    # 编译配方
   ls deepspeed/ops/adam/                         # Python 前端
   ```

3. **需要观察的现象**：步骤 a 的输出中两个条目带 `->` 箭头（symlink 标志）；步骤 b 三条命令都成功，`csrc/adam/` 下是 `.cu/.cpp` 文件，`deepspeed/ops/adam/` 下全是 `.py`。
4. **预期结果**：`csrc/adam/` 包含 CUDA 源文件（如 multi_tensor_adam.cu），`deepspeed/ops/adam/` 包含 `fused_adam.py`、`cpu_adam.py` 等 6 个 Python 文件。具体文件名以 `ls` 实际输出为准。
5. 本实践为纯文件系统检查，无环境依赖。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `deepspeed/ops/triton_ops/` 的算子不需要 `op_builder/` 里对应的 CUDA builder？
**答案**：Triton 内核用 Python 写、由 Triton 编译器在 GPU 上即时编译，不需要 nvcc/C++ 工具链，因此走完全不同的装载路径（还记得 4.3 节的 `HAS_TRITON` 懒加载吗？就是为了它）。

**练习 2**：如果修改了 `csrc/adam/` 下的 CUDA 源码但训练行为没变，最可能的原因是什么？
**答案**：JIT 缓存。运行期加载的是缓存目录里已编译的 `.so`，源码改了但缓存未失效时仍用旧二进制。需要清掉 deepspeed 编译缓存（`~/.cache/deepspeed` 下）或重新预编译——u1-l3 会给出完整安装/重编译流程。

**练习 3**：`csrc/includes/` 和 `csrc/utils/` 这种「公共」目录存在的意义是什么？
**答案**：多个内核子目录共享的头文件与工具函数（如通用宏、类型转换）集中放置，避免每个算子目录复制一份。这与 Python 侧 `deepspeed/utils/` 的定位完全同构——两种语言的仓库用了同一种组织思想。

### 4.5 deepspeed/models 模型族包：一族模型一套训练定制

#### 4.5.1 概念说明

`deepspeed/models/` 是 0.20.x 周期新增的目录（#8659），确立「**模型族包（model family package）**」约定：**一个模型家族一个子包**。首个家族是 `glm_moe_dsa`——Hugging Face transformers 里 GLM-5.2（DeepSeek Sparse Attention 变体）对应的文件夹名。

为什么需要它？因为**有些训练改动只对某一个模型有意义**：为它的注意力写专用内核、替换它的某个模块、修正它的权重布局。这类代码放进通用层都不合适：

| 通用层 | 定位 | 与家族包的区别 |
|---|---|---|
| `deepspeed/ops/` | 跨模型复用的算子（fused Adam 等） | 谁都能用，不含模型知识 |
| `deepspeed/module_inject/` | 按 policy/preset **批量**注入通用机制（AutoTP/AutoEP） | 机制通用，按模型配置适配 |
| `deepspeed/models/<家族>/` | **只对这个家族有意义**的内核/模块/代码 | 别的模型不该 import 它 |

其他训练栈早有类似设计（torchtitan 的 `models/<model>/`、THUDM/slime 的插件目录）；DeepSpeed 推理侧也已有 `deepspeed/inference/v2/model_implementations/<family>/`、AutoEP 有 `module_inject/auto_ep_presets/`——**训练侧此前没有这个位置**，`deepspeed/models/` 补上了它。

约定有四条（全部写在包的 docstring 里，见 4.5.3）：子包名对齐 `transformers.models` 的文件夹名（读者从 HF 模型直达 DeepSpeed 代码，无需查表）；家族包自持所需的一切（内核、替换模块、纯 PyTorch 代码）；`import deepspeed` **不**导入这些子包，家族的可选依赖（这里是 TileLang）只由该家族的用户承担；每个子包暴露「建模后、`deepspeed.initialize` 前调用」的入口函数。

#### 4.5.2 核心流程

**（1）使用方式**：与单元 2 将学的「三步接入法」相比，家族路径只多一步——在 `deepspeed.initialize` **之前**对已建模的模型调用家族入口：

```text
通用路径:  HF 模型 ──→ deepspeed.initialize(model, config)      # 引擎按 config 启用通用机制

家族路径:  HF 模型
             └─ deepspeed.models.glm_moe_dsa.replace_attention(model)   ← 多出的一步
                  └─ deepspeed.initialize(model, config)                # 引擎照常工作；
                                                                         # 参数名/冻结模式不变
```

**（2）加载纪律**（谁付成本，谁导依赖）：

```text
import deepspeed                      → deepspeed/models/ 下什么也不执行（其 __init__.py 只有 docstring）
import deepspeed.models               → 仍不导入任何家族
import deepspeed.models.glm_moe_dsa   → 导入 attention.py（替换模块）；此时仍不需要 TileLang
   （函数体内才 from ...kernels import ... / import tilelang）
                                        → 只有真正调用 replace_attention/跑内核的用户才装 TileLang
```

这与 4.3 节的 `HAS_TRITON` 懒加载是同一思想的两种形态：**把可选成本从「所有 import 者」转移到「真正使用者」**。

#### 4.5.3 源码精读

**（1）约定本体只有 13 行**。[deepspeed/models/__init__.py:5-13](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/__init__.py#L5-L13) 全文就是一个 docstring、零 import：

```python
"""Training changes for specific model families.

One subpackage per model family, named after the model's folder in ``transformers.models`` (for
example ``glm_moe_dsa`` for GLM-5.2). A subpackage holds whatever that family needs to train fast
or to fit in memory: kernels, replacement modules, or plain PyTorch code. ``import deepspeed`` does
not import these subpackages, so their optional dependencies are only needed by the models that
use them. Each subpackage documents the function to call on a built model before
``deepspeed.initialize``.
"""
```

一个「目录级 README」式的 `__init__.py`——4.3 节说 `__init__.py` 定义公共 API 面，这里反过来演示另一个用法：**只声明约定、不导出任何东西**，保证 `import deepspeed` 的开销为零增量。

**（2）家族包的公共面**。[deepspeed/models/glm_moe_dsa/__init__.py:11-14](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/__init__.py#L11-L14)：

```python
from deepspeed.models.glm_moe_dsa.attention import (DeepSpeedGlmMoeDsaAttention, replace_attention,
                                                    set_context_parallel_group)

__all__ = ["DeepSpeedGlmMoeDsaAttention", "replace_attention", "set_context_parallel_group"]
```

三个名字就是整个家族的 API：`replace_attention` 是「建模后、initialize 前」的入口；`set_context_parallel_group` 把一条序列切到多卡（上下文并行）；`DeepSpeedGlmMoeDsaAttention` 是替换模块类。

**（3）内核子包声明自己的依赖边界**。[deepspeed/models/glm_moe_dsa/kernels/__init__.py:21-24](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/kernels/__init__.py#L21-L24) 只导出两个函数（`select_topk` 闪电索引器 + `sparse_mla` 吸收式稀疏注意力内核），其 docstring 末尾（[L18](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/kernels/__init__.py#L18)）明说「导入本包即导入 TileLang」——所以它只被 `attention.py` 在**函数体内**延迟导入（如 [attention.py:226](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/attention.py#L226)、[attention.py:280](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/attention.py#L280)），模块顶层 import 只有 torch 与 deepspeed 自身（[attention.py:39-44](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/attention.py#L39-L44)）。

**（4）为什么是「替换模块」而不是「注册注意力函数」**：[attention.py:8-13](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/attention.py#L8-L13) 的模块 docstring 给出理由——transformers 的原生模块在调用注意力接口**之前**就把 latent 键值解压成逐头 K/V、并让 indexer 生成稠密 `[batch, index_heads, seq, seq]` fp32 打分张量（GLM-5.2 在 32K token 时单是 indexer 就要分配 64 GiB），注册再多的注意力函数也来不及。因此 [attention.py:158](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/attention.py#L158) 定义替换模块（保留源模块全部参数与属性名，只换 forward），[attention.py:393](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/attention.py#L393) 是入口 `replace_attention(model, index_block_rows=8192, warm_up=True)`，并在 [attention.py:404](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/attention.py#L404) 才 `import tilelang`。内核细节、ZeRO-3 external parameter 注册、内核预热等留给 u9-l6。

#### 4.5.4 代码实践

1. **实践目标**：亲手验证 4.5.2 的「加载纪律」，确认 `import deepspeed` 确实不碰 `deepspeed/models/`。
2. **操作步骤**（装好 torch 的环境、仓库根目录下执行）：

   ```bash
   # a) import deepspeed 不加载任何模型族
   python -c "import sys, deepspeed; print([m for m in sys.modules if m.startswith('deepspeed.models')])"

   # b) 导入家族包不需要 TileLang（kernels 是函数内延迟导入的）
   python -c "import sys, deepspeed.models.glm_moe_dsa as g; print(g.__all__); print('tilelang loaded:', 'tilelang' in sys.modules)"

   # c) 看目录与文档
   ls deepspeed/models/ deepspeed/models/glm_moe_dsa/ deepspeed/models/glm_moe_dsa/kernels/
   ```

3. **需要观察的现象**：命令 a 输出空列表 `[]`；命令 b 输出三个 API 名，且 `tilelang loaded: False`；命令 c 能看到 `attention.py` 与 `kernels/` 下的 6 个内核文件。
4. **预期结果**：与上述一致。若命令 b 的 `tilelang loaded` 为 `True`，说明你的环境中 torch 或其他库提前加载了它，属于环境差异——**待本地验证**。命令 a/b 不需要 GPU。
5. 家族包的完整用法文档在 [docs/code-docs/source/models/glm_moe_dsa.rst](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/docs/code-docs/source/models/glm_moe_dsa.rst)（docs 索引经 [docs/code-docs/source/index.rst:60](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/docs/code-docs/source/index.rst#L60) 的 `models/*` 通配收录）。

#### 4.5.5 小练习与答案

**练习 1**：家族子包为什么必须叫 `glm_moe_dsa`，而不是 `glm52` 或 `ds_attention` 这样的「更好记」的名字？
**答案**：约定要求子包名对齐 `transformers.models` 里的文件夹名（GLM-5.2 在 transformers 中就叫 `glm_moe_dsa`）。这样读者从 HF 模型卡片直达 DeepSpeed 代码，无需查表；自造名字会破坏这条「零查表」映射。

**练习 2**：`deepspeed/models/` 与 `deepspeed/ops/` 都放「高性能代码」，怎么判断一段新代码该放哪边？
**答案**：问「这段代码对第二个模型有意义吗？」有意义（如 fused Adam、all-to-all 通信）放 `ops/`（或 csrc + op_builder 三件套）；只对一个家族有意义（如 GLM-5.2 的稀疏注意力内核与替换模块）放 `deepspeed/models/<家族>/`。判据是**适用范围**，不是语言或性能。

**练习 3**：为什么 `attention.py` 把 `from ...kernels import ...` 写在函数体内，而不是文件顶部？
**答案**：kernels 子包一导入就导入 TileLang。若放在顶部，`import deepspeed.models.glm_moe_dsa`（甚至任何间接 import 链）都会要求装 TileLang，「家族可选依赖只由家族用户承担」的约定就被破坏。函数内导入把依赖绑定到「真正调用内核」的时刻——与 4.3 节 `HAS_TRITON` 的模块级 `__getattr__` 异曲同工。

## 5. 综合实践

**任务：制作你自己的《DeepSpeed 代码地图》**——这是本讲的正式产出物，建议保存为 `my-map.md`，后续每一讲都在它上面打勾。

1. **实践目标**：把本讲全部知识点（顶层四分类、主包子模块、runtime 内部、ops/csrc/op_builder 三件套、models 模型族包）固化为一份你亲手标注的地图，并与本手册的单元规划对齐。
2. **操作步骤**：
   1. 在仓库根目录执行 `tree -L 2 deepspeed/ > my-map.txt`（若未装 tree，用 `find deepspeed -maxdepth 1` 或 `ls -d deepspeed/*/` 等价替代）；
   2. 对输出的**每个二级目录**（`deepspeed/accelerator`、`deepspeed/comm`、`deepspeed/runtime`……共 23 个）写一行职责注释，格式建议：`runtime/  # 训练引擎+ZeRO+流水线（单元 2/4/5/6/8）`；
   3. 在每行末尾标注该目录对应本手册哪个单元（对照 4.2.2 的分层图）；
   4. 单独追加一节「三件套对照表」：任选 3 个算子（建议 adam、aio、transformer），列出它们在 `csrc/`、`op_builder/`、`deepspeed/ops/` 三处的文件路径；
   5. 再追加「模型族包」一节：阅读 [deepspeed/models/__init__.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/__init__.py) 的约定说明（13 行），用自己的话总结模型族包与 ops/module_inject 两层的分工（提示：判据是代码的适用范围——跨模型 vs 单一模型家族，见 4.5.5 练习 2）；
   6. 最后追加「symlink 清单」：记录 4.4.4 步骤 a 观察到的 3 个符号链接。
3. **需要观察的现象**：`tree -L 2` 的输出中，`deepspeed/ops/` 会展开出 `csrc` 和 `op_builder` 两个条目——它们正是 symlink 指向的顶层目录内容的投影，这会在你的地图上直观呈现「双轨」；`deepspeed/models/` 下只有一个家族目录 `glm_moe_dsa/`。
4. **预期结果**：一份 23+ 行的注释地图 + 三件套对照表 + 模型族包小结 + symlink 清单。完成后自测：随机指着地图上任一目录，你能不假思索地说出「它属于哪一层、哪个单元会讲它」——通过自测即完成本讲。
5. 本实践全程只读，不依赖 GPU；`tree` 命令输出细节随版本略有差异，属正常现象。

## 6. 本讲小结

- 仓库顶层按「使用时刻」分四类：运行期核心 `deepspeed/`、安装期工具 `op_builder/` 与 `accelerator/`、内核源码 `csrc/`、工程设施 `bin/`/`tests/`/`docs/` 等。
- `deepspeed/runtime/` 是训练运行时的心脏：引擎、配置、ZeRO、流水线、混合精度优化器都在其子树，是单元 2/4/5/6/8 的主战场；文件名≈功能名，命名直白。
- 公共 API 面由 [deepspeed/__init__.py:44-72](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/__init__.py#L44-L72) 的 import 块定义，日常入口其实只有 `initialize()` 与 `init_inference()` 两个函数。
- 懒加载是包入口的两条硬约束的实现手段：`HAS_TRITON` 经模块级 `__getattr__` 延迟计算、`dist = None` 延迟到 `initialize()` 才绑定，且 import 全程不得创建 CUDA 上下文。
- 一个高性能算子 = 三件套：`csrc/`（CUDA 实现）+ `op_builder/`（编译配方）+ `deepspeed/ops/`（Python 前端）；顶层目录经 3 个 symlink 服务包内路径，Windows 下由 setup.py 物理拷贝兜底。
- 本周期新增 `deepspeed/models/` 模型族包（首个家族 `glm_moe_dsa`，对应 GLM-5.2）：只对一个模型家族有意义的训练定制住这里；`import deepspeed` 不导入它，家族可选依赖（TileLang）只由家族用户承担，入口函数在建模后、`deepspeed.initialize` 前调用。

## 7. 下一步学习建议

下一讲 **u1-l3《安装与构建：setup.py 与 CUDA 算子编译》**将沿着本讲铺好的路走进 `setup.py` 与 `op_builder/builder.py`，弄清「预编译 vs JIT」两条算子编译路径的分岔点——本讲 4.4 节的三件套在那里变成可运行的命令。

在进入下一讲之前，建议先做两件热身事：

1. 通读 [docs/_pages/training.md](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/docs/_pages/training.md)，把文档里的 API 名字（`initialize`、`engine.backward`、`engine.step`）与本讲 4.3 的 API 面对上号。
2. 用 `git log --oneline -5 -- deepspeed/runtime/zero/` 感受一下 ZeRO 子系统的活跃度——单元 5 你将深入这些频繁变动的文件，而现在你已知道它们在地图上的位置。

另外两个远期路标：本周期新增的模型族包将在 **u9-l6** 以 `glm_moe_dsa` 为例完整精读（TileLang 稀疏注意力内核、ZeRO-3 external parameter、上下文并行）；`deepspeed/models/__init__.py` 里提到的「调用时机在建模后、initialize 前」的语义，会随着单元 2 的学习自然清晰。
