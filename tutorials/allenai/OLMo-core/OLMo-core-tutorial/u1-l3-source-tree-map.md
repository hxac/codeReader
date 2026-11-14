# 源码地图：目录结构与测试布局

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出（或默写）`src/olmo_core` 下各子包的职责一览图，说出 `nn/`、`data/`、`train/`、`distributed/`、`optim/` 等子包分别负责什么。
2. 说明 `src/test` 如何**镜像** `src/olmo_core` 的源码结构，并能根据一个源码文件立刻猜到它对应测试文件的位置。
3. 面对「模型、数据、训练、分布式」四类问题，能在源码树中快速定位到对应的目录和文件，而不是在几千个文件里盲目搜索。

本讲是纯「认路」讲：不深入任何算法，只建立一张心智地图。后面所有讲义都会反复引用这张地图上的地名。

## 2. 前置知识

- 你已经完成 u1-l2 的开发安装（`pip install -e '.[all]'`），能在本地运行 `pytest`。本讲大部分实践只需要能读到仓库文件，不强制需要 GPU。
- 了解 Python 的「包（package）」概念：一个含有 `__init__.py` 的目录就是一个包，`import olmo_core.train` 时 Python 会执行 `olmo_core/train/__init__.py`。
- 了解「src 布局」这种工程惯例：真正的包不放在仓库根目录，而是放在 `src/` 下面，避免「测试时误 import 了本地目录而非已安装版本」的问题。
- 回顾 u1-l1 的结论：仓库分四大区域——`src/olmo_core`（库本体）、`src/test`（测试）、`src/scripts`（训练脚本）、`src/examples`（示例）。本讲会把这张粗地图细化。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/olmo_core/__init__.py` | 库的根入口。出人意料地「轻」：不导出任何模型或训练类，只做环境准备工作（mxfp8 默认 scale、Triton 缓存目录） |
| `src/olmo_core/config.py` | `Config` 基类定义处，整个库「一切皆 Config」的源头 |
| `src/olmo_core/nn/config.py` | `ModuleConfig` / `ModelConfig`，模型配置的中间基类 |
| `src/olmo_core/nn/transformer/config.py` | `TransformerConfig`（模型类配置）定义处 |
| `src/olmo_core/train/config.py` | `TrainerConfig`（训练类配置）定义处 |
| `src/olmo_core/data/numpy_dataset.py` | `NumpyDatasetConfig`（数据类配置）定义处 |
| `src/olmo_core/train/__init__.py` | `train` 子包的「门面」：模块文档、公开 API 再导出、训练环境准备函数 |
| `src/test/train/trainer_test.py` | 测试镜像结构的最小示例 |
| `src/test/nn/moe/top16_native_test.py` | GPU 测试的跳过机制示例 |
| `src/examples/llm/train.py` | 通过它的 import 语句验证「按子包导入」的使用方式 |

## 4. 核心概念与源码讲解

### 4.1 仓库的 src 布局：五大区域

#### 4.1.1 概念说明

进入仓库的 `src/` 目录，只有六个条目（一个文件 + 五个目录）：

```text
src/
├── Dockerfile            # 两阶段构建的 Docker 镜像定义（u1-l2 已讲）
├── olmo_core/            # ① 库本体：所有可复用的"积木"
├── test/                 # ② 单元测试：与 olmo_core 一一镜像
├── scripts/              # ③ 训练/工具脚本：official/（官方复现）、train/（内部模板）、beaker/、data/ 等
├── examples/             # ④ 可运行示例：llm/、moe/、huggingface/、ngpt/、olmo_ddp/ 等
└── integration_tests/    # ⑤ 集成测试：端到端跑小模型（test_train_small_model.py）
```

这五个区域的**角色分工**是理解这个仓库的第一把钥匙：

- **① 是「产品」**：PyPI 上发布的 `ai2-olmo-core` 包，内容就是 `src/olmo_core`。
- **② 是①的安全网**：改了①里的代码，先跑②确认没破坏行为。
- **③ 是①的「菜谱」**：用①的积木拼出 OLMo-2/OLMo-3 官方训练配置。
- **④ 是①的「教学版菜谱」**：比③更小、更自包含，适合人类阅读学习（u1-l4 的主角）。
- **⑤ 是①的「整机测试」**：跨模块端到端验证，而不像②那样逐模块验证。

#### 4.1.2 核心流程

拿到任何一个 GitHub 上的 Python 库仓库，建议按下面的顺序「扫图」：

1. 看仓库根：`README.md`、`pyproject.toml`（依赖与工具配置）、`Makefile`（常用命令入口）。
2. 看 `src/`（或包根）：确认包名与顶层布局。
3. 打开包的 `__init__.py`：判断这个库是「根入口导出一切」还是「按子包分别导入」。
4. 列出包的一级子目录，给每个子目录贴一张职责标签。
5. 找到测试目录，确认它和源码的对应规则。

本讲的 4.1 → 4.4 就是对 OLMo-core 执行这五步的过程。

#### 4.1.3 源码精读

先看第 3 步：OLMo-core 的根入口非常「轻」。[src/olmo_core/__init__.py:L7-L9](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/__init__.py#L7-L9) 只是在导入时解析一次 mxfp8 的默认 scale 模式环境变量；[src/olmo_core/__init__.py:L61](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/__init__.py#L61) 调用了 `_default_triton_cache_dir()`，这个函数（[src/olmo_core/__init__.py:L17-L58](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/__init__.py#L17-L58)）为每个本地 rank 分配独立的 Triton 编译缓存目录，避免多进程同时编译内核时互相踩踏缓存。

**这个文件里没有任何模型类、训练类。** 它传递了一个重要信号：OLMo-core 是「按子包使用」的库——用户写 `from olmo_core.train import TrainerConfig`，而不是 `from olmo_core import TrainerConfig`。这一点可以拿官方示例验证，[src/examples/llm/train.py:L18-L35](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py#L18-L35) 中的 import 全部形如 `from olmo_core.data import ...`、`from olmo_core.nn.transformer import TransformerConfig`、`from olmo_core.train import ...`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认五大区域的划分，破除「入口一定在根 `__init__.py`」的误解。
2. **操作步骤**：
   - 在仓库根执行 `ls src/`，对照 4.1.1 的树状图逐项确认。
   - 再执行 `cat src/olmo_core/__init__.py`（或用编辑器打开），数一数里面的 `import`：你会发现没有一行导出模型或训练符号。
   - 完成过 u1-l2 安装的读者可以执行 `python -c "import olmo_core; print(olmo_core.__file__)"`，确认安装方式（可编辑安装会指向你的仓库路径）。
3. **需要观察的现象**：`src/` 下恰好是五个目录加一个 Dockerfile；根 `__init__.py` 只有环境准备逻辑。
4. **预期结果**：与 4.1.1 的树完全一致。`python -c ...` 的输出路径**待本地验证**（取决于你的安装方式）。

#### 4.1.5 小练习与答案

**练习 1**：`src/scripts/official/` 和 `src/examples/llm/` 里都是「训练脚本」，它们的定位有何不同？

<details><summary>参考答案</summary>

`scripts/official/` 用于**复现已发布的模型**（如 OLMo-2 32B、OLMo-3 7B），配置完整、面向集群；`examples/` 是**教学示例**，自包含、体量小，适合阅读和本地试跑。前者是生产菜谱，后者是入门教材。

</details>

**练习 2**：为什么 `src/olmo_core/__init__.py` 里要处理 Triton 缓存目录，而不是放到某个用到 Triton 的模块里？

<details><summary>参考答案</summary>

因为它需要在**任何子包被导入之前**、且只执行一次地设置好环境变量 `TRITON_CACHE_DIR`。根 `__init__.py` 是 `import olmo_core.*` 路径上必然最先执行的代码，把环境准备放在这里可以保证无论用户先导入哪个子包，缓存隔离都已生效（函数内部还会尊重用户显式设置的 `TRITON_CACHE_DIR`）。

</details>

### 4.2 src/olmo_core 子包职责一览：画出寻路地图

#### 4.2.1 概念说明

`src/olmo_core` 根目录下有两种东西：**散装模块文件**（根目录的 `.py`）和**子包**（带 `__init__.py` 的目录）。先看散装文件：

| 文件 | 职责 |
| --- | --- |
| `config.py` | `Config` 基类：一切配置的基石（u2-l1 主角） |
| `utils.py` | 杂项工具：`seed_all`、`prepare_cli_environment`、`get_default_device` 等 |
| `io.py` | 输入输出：`add_cached_path_clients` 等，支持多种远程文件系统 |
| `script_utils.py` | 官方脚本的通用入口：`ExperimentConfig` 与 `main()` 模式（u2-l3） |
| `aliases.py` / `exceptions.py` / `version.py` / `doc_utils.py` / `fs_cache.py` / `mxfp8_config.py` / `_nvtx.py` | 别名、异常、版本号、文档工具、文件缓存、mxfp8 默认值、NVTX 性能标注 |

再看子包。按下表给每个子包贴上「一张标签」，这张表就是本讲要求你画出的职责一览图：

| 子包 | 一句话职责 | 典型内容 |
| --- | --- | --- |
| `nn/` | **模型**：网络结构与算子 | `transformer/`、`attention/`、`moe/`、`vision/`、`feed_forward.py`、`layer_norm.py`、`rope.py`、`lm_head.py`、`embedding.py`、`conversion/`（权重转换） |
| `data/` | **数据**：数据集与加载器 | `numpy_dataset.py`、`data_loader.py`、`collator.py`、`tokenizer.py`、`composable/`（新数据 API）、`mixes/`（预置配方）、`multimodal/` |
| `train/` | **训练**：训练循环与扩展 | `trainer.py`、`config.py`、`common.py`（Duration 等枚举）、`callbacks/`、`train_module/`、`checkpoint.py` |
| `distributed/` | **分布式**：并行与分布式 checkpoint | `parallel/`（`data_parallel.py`、`tensor_parallel.py`、`pipeline_parallel.py`、`context_parallel.py`、`expert_parallel.py`）、`checkpoint/`、`utils.py`、`nn.py` |
| `optim/` | **优化器** | `adamw.py`、`lion.py`、`muon.py`、`scheduler.py`（学习率调度）、`skip_step_optimizer.py` |
| `eval/` | **评测**：困惑度等 | `evaluator.py`、`lm_evaluator.py`、`metrics.py`、`task_groups.py` |
| `generate/` | **生成**：自回归推理 | `generation_module/`、`sampling.py`、`chat.py` |
| `launch/` | **部署**：Beaker 集群启动 | `beaker.py`、`beaker_presets.py` |
| `kernels/` | **内核**：自定义 CUDA/Triton 算子 | `moe.py`、`grouped_mm.py`、`swiglu.py` 等 |
| `float8/` / `ops/` / `internal/` / `testing/` | 低精度训练、轻量算子、内部 API、测试工具 | —— |

#### 4.2.2 核心流程

「模型、数据、训练、分布式」四类问题的**定位口诀**：

```text
看结构（层、注意力、MoE、视觉）      → nn/
喂数据（.npy 数据集、加载器、混合）   → data/
管训练（循环、回调、断点续训）        → train/
多卡多机（FSDP/TP/PP、分布式存取）   → distributed/
```

再配一条**从属规则**：子包内部继续按「名词分目录、动词分文件」细分。例如 `nn/transformer/` 下，`model.py` 是整体、`block.py` 是积木块、`config.py` 是配置、`init.py` 是初始化。也就是说，`config.py` 这个文件名几乎会出现在**每个**子包里——它是该子包「Config 门类」的定义处，这正是 u1-l1 埋下的「一切皆 Config」伏笔在目录结构上的投影。

#### 4.2.3 源码精读

所有 Config 门类的共同祖先是 [src/olmo_core/config.py:L56-L71](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L56-L71) 定义的 `Config` 基类，它的 docstring 明确要求：子类必须仍用 `@dataclass` 装饰、字段只能用可序列化的简单类型或嵌套 Config。

模型这条线还有一层中间基类：[src/olmo_core/nn/config.py:L10-L20](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/config.py#L10-L20) 定义了 `ModuleConfig`（L10）与 `ModelConfig`（L20），后者是 `TransformerConfig` 的直接父类。于是可以画出三条「Config 谱系」：

```text
模型：  Config → ModuleConfig → ModelConfig → TransformerConfig   (nn/transformer/config.py)
数据：  Config → NumpyDatasetConfig(抽象基类) → 各具体数据集 Config (data/numpy_dataset.py)
训练：  Config → TrainerConfig                                (train/config.py)
```

「模型、数据、训练」三大类 Config 的谱系都终止于 `config.py` 这一个根——这就是「一张地图读懂全库设计」的含义。

#### 4.2.4 代码实践

1. **实践目标**：把 4.2.1 的表格从「读到的」变成「自己验证过的」。
2. **操作步骤**：
   - 依次执行 `ls src/olmo_core/nn`、`ls src/olmo_core/data`、`ls src/olmo_core/train`、`ls src/olmo_core/distributed`、`ls src/olmo_core/optim`。
   - 对照上表，给每个列出的条目口头贴标签；发现表格没写的条目（例如 `nn/_csrc`、`data/misc`），猜一下职责再点进去看 `__init__.py` 或文件头 docstring 求证。
3. **需要观察的现象**：`train/` 下有 `callbacks/` 和 `train_module/` 两个子目录；`distributed/parallel/` 下恰好有五个并行维度文件。
4. **预期结果**：与 4.2.1 表格一致；`nn/_csrc` 是自定义 CUDA 扩展源码目录（配合 `kernels/` 的构建脚本），`data/misc` 放少量杂项数据工具。

#### 4.2.5 小练习与答案

**练习 1**：想给训练过程加一个「每 100 步把 loss 画成图」的功能，应该去哪个目录找参考实现？

<details><summary>参考答案</summary>

`src/olmo_core/train/callbacks/`。训练过程中的周期性行为（记录、评估、存档）都通过回调实现，那里有速度监控、日志、checkpointer 等现成范例（u6-l3 详讲）。

</details>

**练习 2**：`config.py` 文件名在多少个子包里重复出现？这说明什么？

<details><summary>参考答案</summary>

在 `olmo_core` 根、`nn/`、`nn/transformer/`、`nn/moe/`、`nn/vision/`、`train/`、`optim/` 等许多地方出现（可用 `Glob` 模式 `src/olmo_core/**/config.py` 验证）。这说明「每个功能门类都有自己的 Config」是全库统一的组织约定——配置与实现放在同一目录，就近维护。

</details>

**练习 3**：`distributed/parallel/` 与 `distributed/checkpoint/` 为什么分成两个子包？

<details><summary>参考答案</summary>

`parallel/` 解决「计算如何切到多卡」（DP/TP/PP/CP/EP 五种维度），`checkpoint/` 解决「分布式状态如何存取」（分片保存、重分片恢复、多种文件系统后端）。二者关注点不同：前者管前向/反向的通信，后者管持久化，所以独立成包。

</details>

### 4.3 子包的门面：精读 train/__init__.py

#### 4.3.1 概念说明

既然库按子包组织，那么每个子包的 `__init__.py` 就是它的**门面（facade）**：外部用户只需要认识门面导出的符号，不必知道内部文件切分。OLMo-core 的子包门面通常包含四样东西：

1. 模块级 docstring（功能介绍 + 最小用法示例 + API Reference 链接）；
2. 从内部模块导入核心符号；
3. `__all__` 列出公开 API；
4. 少量门面级函数（如环境准备）。

#### 4.3.2 核心流程

以 `train` 子包为例，它的 docstring 给出了训练脚本的**标准骨架**（[src/olmo_core/train/__init__.py:L23-L35](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L23-L35)）：

```python
if __name__ == "__main__":
    prepare_training_environment()

    # Build train module and data loader...

    # Build trainer.
    trainer = trainer_config.build(train_module, data_loader)

    # Run the trainer.
    trainer.fit()

    # Clean up.
    teardown_training_environment()
```

这就是贯穿全库的训练生命周期：**准备环境 → 组装组件 → `fit()` 训练 → 清理环境**。u1-l4 的示例、u9 的官方脚本，都长这个样子。

#### 4.3.3 源码精读

- [src/olmo_core/train/__init__.py:L1-L41](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L1-L41)：模块 docstring，概述异步 checkpoint、任意并行策略、异步指标收集、回调系统等特性——相当于这个子包的「产品说明书」。
- [src/olmo_core/train/__init__.py:L43-L64](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L43-L64)：从 `..distributed.utils`、`.checkpoint`、`.common`、`.config`、`.trainer` 等内部模块把 `Trainer`、`TrainerConfig`、`Checkpointer`、`Duration`、`LoadStrategy` 等符号汇集到门面。注意 `from ..distributed.utils import ...`——子包之间也会互相引用（train 依赖 distributed）。
- [src/olmo_core/train/__init__.py:L66-L79](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L66-L79)：`__all__` 明确列出 12 个公开名字，这就是这个子包对外的全部「官方接口」。
- [src/olmo_core/train/__init__.py:L85-L148](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L85-L148)：`prepare_training_environment()`——设置多进程 spawn 模式、初始化分布式进程组、配置日志、初始化随机种子。docstring 特别提示它应在 `if __name__ == "__main__":` 块的最开头调用。
- [src/olmo_core/train/__init__.py:L151-L156](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L151-L156)：`teardown_training_environment()`——销毁进程组，训练收尾。

读门面是「低成本了解一个子包」的最佳入口：先读 docstring 知道它能干什么，再看 `__all__` 知道它暴露什么，最后顺着 import 语句跳进内部文件深读。

#### 4.3.4 代码实践

1. **实践目标**：验证「门面再导出」机制——你在门面拿到的类，真正的定义文件在别处。
2. **操作步骤**（完成 u1-l2 安装后，在仓库根执行）：

   ```bash
   python -c "from olmo_core.train import TrainerConfig; print(TrainerConfig.__module__)"
   ```

3. **需要观察的现象**：输出的不是 `olmo_core.train`，而是 `olmo_core.train.config`——即定义所在的真正模块。
4. **预期结果**：输出 `olmo_core.train.config`（Python 的 `__module__` 属性记录类的定义处，与导入路径无关；**待本地验证**）。再用同样的方法打印 `TransformerConfig.__module__` 和 `NumpyDatasetConfig.__module__`，为综合实践做准备。

#### 4.3.5 小练习与答案

**练习 1**：`from olmo_core.train import Duration` 中的 `Duration` 最初定义在哪个文件？

<details><summary>参考答案</summary>

从 [src/olmo_core/train/__init__.py:L55-L62](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/__init__.py#L55-L62) 可见它来自 `.common`，即 `src/olmo_core/train/common.py`。也可用 `python -c "from olmo_core.train import Duration; print(Duration.__module__)"` 验证。

</details>

**练习 2**：为什么 `prepare_training_environment()` 要求放在 `if __name__ == "__main__":` 块开头？

<details><summary>参考答案</summary>

因为它会初始化分布式进程组并设置多进程 spawn 启动方式。spawn 模式下子进程会重新 import 你的脚本，如果环境准备写在模块顶层，就会在子进程里被重复/递归执行；放在 `__main__` 守卫内可保证只有主入口执行一次，子进程由分布式运行时另行管理。

</details>

### 4.4 src/test：与源码镜像的测试布局

#### 4.4.1 概念说明

`src/test` 的组织规则只有一条：**镜像 + 改名**。

- 目录镜像：`olmo_core/<子包>/` ↔ `test/<子包>/`（如 `olmo_core/nn/transformer/` ↔ `test/nn/transformer/`）。
- 文件改名：源码文件 `foo.py` 对应测试文件 `foo_test.py`（注意后缀是 `_test`，不是 `test_` 前缀）；包根的散装文件对应 `test/` 根下的 `foo_test.py`。

几个真实对照：

| 源码 | 测试 |
| --- | --- |
| `olmo_core/config.py` | `test/config_test.py` |
| `olmo_core/utils.py` | `test/utils_test.py` |
| `olmo_core/data/numpy_dataset.py` | `test/data/numpy_dataset_test.py` |
| `olmo_core/nn/transformer/block.py` | `test/nn/transformer/block_test.py` |
| `olmo_core/nn/transformer/{model,config,init}.py` | `test/nn/transformer/{model,config,init}_test.py` |
| `olmo_core/train/trainer.py` | `test/train/trainer_test.py` |

注意镜像**不追求严格一一对应**：`test/nn/transformer/` 里多了一个源码中没有同名文件的 `ac_rng_test.py`（专测 RNG 状态保持）；`test/train/` 下也没有 `config_test.py`（`TrainerConfig` 的行为由 `trainer_test.py` 等间接覆盖，该文件当前只保留了一个纯函数测试）。规则是「默认镜像，按测试需要增删」。

#### 4.4.2 核心流程

测试文件的内部约定（AGENTS.md 明文规定）：

1. 测试函数命名 `test_*`。
2. 优先用 `@pytest.mark.parametrize` 覆盖多组输入，而不是复制粘贴多个相似测试。
3. GPU 测试用 `@pytest.mark.gpu` 标记（仅供 CI 用 `-m` 筛选），**标记本身不产生跳过**，真正的跳过靠函数体内的 `pytest.skip()` 和模块级的 `pytest.importorskip()`（u1-l2 已建立这一认知）。

于是「找一个功能的测试」的流程是：

```text
想测 X → 定位 X 的源码文件 foo.py（用 4.2 的地图）
       → 猜测试路径 test/<同目录>/foo_test.py
       → 若不存在，在 test/<同目录>/ 里按主题搜（Grep "X"）
```

#### 4.4.3 源码精读

- [src/test/train/trainer_test.py:L1-L12](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/train/trainer_test.py#L1-L12)：一个完整的最小测试文件，只有 12 行——导入被测函数 `from olmo_core.train.trainer import _metric_value_to_tensor`，然后断言超大 Python 整数转张量后不丢失精度。它展示了镜像规则下最朴素的形态：测 `trainer.py` 里的一个具体函数。
- [src/test/nn/transformer/block_test.py:L78-L91](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/nn/transformer/block_test.py#L78-L91)：`parametrize` 约定的典范——同一个测试函数 `test_tensor_parallel_transformer_block` 叠了三层 `@pytest.mark.parametrize`（注意力后端 × 注意力参数 × block 类别），一份代码覆盖十几组组合。
- [src/test/nn/moe/top16_native_test.py:L6-L12](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/test/nn/moe/top16_native_test.py#L6-L12)：GPU 跳过机制的典范——L6 用 `pytest.importorskip("olmo_core.ops.moe_top16")` 应对可选 CUDA 扩展缺失，L9 打 `@pytest.mark.gpu` 标记，L11-L12 在函数体内 `pytest.skip("CUDA required")`。三层防线共同保证无 GPU 环境下静默跳过而非报错。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证「镜像 + 改名」规则和测试跳过机制。
2. **操作步骤**：
   - 在仓库根执行 `ls src/test/nn/transformer/`，与 `ls src/olmo_core/nn/transformer/` 并排对照。
   - 执行 `pytest -v src/test/train/trainer_test.py`（u1-l2 已装好环境）。
   - 执行 `pytest -v src/test/nn/moe/top16_native_test.py`，观察跳过原因。
3. **需要观察的现象**：第一组命令显示两边文件名前缀一一对应（`block` ↔ `block_test` 等）；第二条 pytest 应显示 1 passed；第三条应显示 skipped 并给出原因（importorskip 的模块缺失原因或 "CUDA required"）。
4. **预期结果**：如上；具体 skip 原因取决于你环境里 CUDA 扩展是否构建成功，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`olmo_core/optim/lion.py` 的测试文件路径是什么？

<details><summary>参考答案</summary>

按规则应为 `src/test/optim/lion_test.py`。可用 `ls src/test/optim/` 验证。

</details>

**练习 2**：为什么测试文件用 `foo_test.py` 后缀而不是更常见的 `test_foo.py` 前缀？这对运行测试有什么影响？

<details><summary>参考答案</summary>

这是项目的历史约定（与 Google 风格一致），AGENTS.md 与 pyproject.toml 的 pytest 配置都按此组织。对运行方式的影响：pytest 的默认发现规则同时匹配 `test_*.py` 与 `*_test.py`，所以 `pytest -v src/` 不受影响；但如果你自己写 glob 或 CI 脚本去收集测试文件，必须用 `*_test.py` 模式，否则会一个都匹配不到。

</details>

## 5. 综合实践

**任务**：对照本讲的结构图，在本地仓库中找到 `TransformerConfig`、`TrainerConfig`、`NumpyDatasetConfig` 三个类的定义文件，并记录它们的文件路径和行号。

**操作步骤**：

1. 用 `git grep` 定位（在 git 仓库里比裸搜索更快更准）：

   ```bash
   git grep -n "^class TransformerConfig" -- src/olmo_core
   git grep -n "^class TrainerConfig" -- src/olmo_core
   git grep -n "^class NumpyDatasetConfig" -- src/olmo_core
   ```

2. 对每个类，打开文件核对：`class` 语句上一行通常是 `@dataclass` 装饰器；再向上翻看它的父类。
3. 用 4.3.4 的 `__module__` 方法交叉验证（`from olmo_core.nn.transformer import TransformerConfig` 等）。

**参考答案**（以当前 HEAD `075a7892d` 为准）：

| 类 | 定义位置 | 基类 |
| --- | --- | --- |
| `TransformerConfig` | [src/olmo_core/nn/transformer/config.py:L335-L336](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/nn/transformer/config.py#L335-L336)（`@dataclass` 在 L335，`class` 在 L336） | `ModelConfig` |
| `TrainerConfig` | [src/olmo_core/train/config.py:L26](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/train/config.py#L26) | `Config` |
| `NumpyDatasetConfig` | [src/olmo_core/data/numpy_dataset.py:L2610-L2611](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/data/numpy_dataset.py#L2610-L2611)（`@dataclass(kw_only=True)` 在 L2610，`class` 在 L2611） | `Config, ABC`（抽象基类） |

**延伸观察**：三个类分别属于「模型 / 训练 / 数据」三个世界，但谱系最终都汇聚到 [src/olmo_core/config.py:L56](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/olmo_core/config.py#L56) 的 `Config`——这就是下一单元要展开的「一切皆 Config」设计。另外注意 `numpy_dataset.py` 有 2600 多行、`NumpyDatasetConfig` 定义在文件**末尾**（先实现各个具体数据集类、再抽象出公共配置），而 `train/config.py` 的 `TrainerConfig` 在文件**开头**——同一项目里文件内部组织也各有风格，读源码时要灵活。

## 6. 本讲小结

- `src/` 下五大区域各司其职：`olmo_core/`（库本体）、`test/`（单元测试）、`scripts/`（训练脚本）、`examples/`（示例）、`integration_tests/`（端到端测试），外加 `Dockerfile`。
- 库的根 `__init__.py` 不导出任何功能类，只做环境准备；OLMo-core 是「按子包导入」的库，子包门面（`__init__.py` = docstring + 再导出 + `__all__` + 门面函数）是了解每个子包的最佳入口。
- 四类问题的定位口诀：模型 → `nn/`，数据 → `data/`，训练 → `train/`，分布式 → `distributed/`；每个子包内几乎都有自己的 `config.py`，「一切皆 Config」体现在目录结构上。
- `src/test` 按「目录镜像 + `foo.py` → `foo_test.py` 改名」组织，默认一一对应但允许增删；测试约定为 `test_*` 函数、`parametrize` 优先、GPU 测试靠标记 + `pytest.skip` + `importorskip` 三层机制自动跳过。
- 三大 Config 的定义位置：`TransformerConfig`（`nn/transformer/config.py`）、`TrainerConfig`（`train/config.py`）、`NumpyDatasetConfig`（`data/numpy_dataset.py`），谱系都汇聚到 `config.py` 的 `Config` 基类。

## 7. 下一步学习建议

- 下一讲（u1-l4）将精读 [src/examples/llm/train.py](https://github.com/allenai/OLMo-core/blob/075a7892d8a004242b085bf3c71b996389fd863c/src/examples/llm/train.py)，你会看到本讲地图上的 `nn/`、`data/`、`optim/`、`train/` 四个子包如何被一个脚本组装成完整训练实验——请先自己通读一遍该文件开头的 import 块。
- 推荐随手翻阅两个「门面 docstring 写得很好」的文件加深对子包边界的认识：`src/olmo_core/train/__init__.py`（本讲已读）和 `src/olmo_core/data/composable/__init__.py`（新一代数据 API 的说明书，u4-l2 主角）。
- 带着一个问题预习：`TrainerConfig.build()` 返回的 `Trainer` 在 `trainer.py` 里是如何驱动 `train_module` 和 `data_loader` 的？这是单元六的核心问题。
