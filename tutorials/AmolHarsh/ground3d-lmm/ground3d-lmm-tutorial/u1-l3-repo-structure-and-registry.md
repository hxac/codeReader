# u1-l3 仓库结构与 OpenMMLab 注册机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 Ground3D-LMM 仓库中 `configs/`、`uniseg3d/`、`tools/`、`docs/`、`data/` 五个目录（以及根目录脚本）之间的职责关系图，说出「改一个东西该去哪个目录」。
2. 解释 `custom_imports = dict(imports=['uniseg3d'])` 这一行配置如何触发整个模型包的注册，理解「Python import 的副作用」这一 OpenMMLab 框架的基石机制。
3. 追踪配置文件里的 `type='Grounded_LMM_main'` 字符串，一步步解析到 `uniseg3d/uniseg3d.py` 里的具体 Python 类，并能自己完成整条「配置 → 注册表 → 类」的追踪。
4. 掌握 mmengine Config 的三种叠加手法：`_base_` 继承（含跨包的 `mmdet3d::` 语法）、字段覆盖、`_delete_` 删除，理解为什么 `_3d` 变体配置只需要写 3 个字段。

## 2. 前置知识

本讲需要以下背景概念，都用通俗语言解释一遍。已学过 u1-l1（项目总体架构）的读者可以直接把它们对接到 Ground3D-LMM 上。

### 2.1 什么是「注册表（Registry）模式」

研究代码经常遇到一个问题：配置文件里写的是字符串（比如 `type='SpConvUNet'`），而程序需要的是真正的 Python 类（`uniseg3d.spconv_unet.SpConvUNet`）。**注册表**就是一座「字符串 → 类」的字典：

```python
# 伪代码，帮助理解
MODELS = {
    'SpConvUNet':            SpConvUNet,            # 点云编码器
    'Grounded_LMM_main':     Grounded_LMM_main,     # 模型主入口
    'Grounded_Decoder_Joint': Grounded_Decoder_Joint,  # 查询解码器
    ...
}

model = MODELS['Grounded_LMM_main'](**model_cfg)   # 用字符串查字典，拿到类并实例化
```

注册表的好处：配置文件完全声明式，换模型、换数据集只需要改字符串，不用改任何 Python 代码。OpenMMLab 全家桶（mmengine、mmcv、mmdet、mmdet3d）都建立在这套机制上，Ground3D-LMM 作为一个「挂载在 mmdet3d 上的插件包」，同样靠它接入。

### 2.2 关键前提：Python import 是有副作用的

Python 执行 `import uniseg3d` 时，会**从头到尾执行** `uniseg3d/__init__.py` 里的每一条语句，而这些语句又会触发其他模块的执行。模块顶层写的 `@MODELS.register_module()`（装饰器语法）就在这个执行过程中把类塞进注册表。

也就是说：**只要一个模块被 import 过一次，它顶层注册过的类就全局可用**——不管你 import 的是模块里的哪个名字。这是理解 `custom_imports` 的全部前提。

### 2.3 什么是「配置即代码（config as code）」

Ground3D-LMM 的配置文件是 `.py` 而不是 `.json`/`.yaml`，因为 Python 配置可以写变量、循环和列表推导。比如训练配置里主干网络的通道数：

```python
num_planes=[num_channels * (i + 1) for i in range(5)]  # [32, 64, 96, 128, 160]
```

mmengine 的 `Config.fromfile()` 会执行这个文件、收集所有顶层变量，然后支持 `_base_` 继承与递归合并——本讲 4.3 节详细拆解。

### 2.4 与前两讲的衔接

- u1-l1 已经介绍过四组件架构（SpConvUNet → 超点池化 → Qwen3-VL → 查询解码器）；本讲回答的问题是：**这四个组件在代码里分别放在哪、配置怎么把它们拼起来**。
- u1-l2 装好的环境里已经装了本包（`pip install -e .`）；本讲的代码实践会用到那个环境里的 mmengine / mmdet3d。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [README.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md) | 项目总说明，含「Repository structure」一节，是目录职责的官方口径 |
| [uniseg3d/\_\_init\_\_.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/__init__.py) | 包入口，19 行 import 语句级联触发全部注册（本讲的主角） |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 训练配置：`_base_` 继承、`custom_imports`、模型/数据/优化器声明 |
| [configs/ground3dlmm_eval_ground3d_scannet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py) | Joint 变体评估配置，展示同样的声明套路 |
| [configs/ground3dlmm_eval_ground3d_scannet_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py) | 3D-only 变体配置：只覆盖 3 个字段的「极简继承」范例 |
| [pyproject.toml](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/pyproject.toml) | 声明本包只打包 `uniseg3d` 一个目录 |
| [tools/train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py) / [tools/test.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py) | 训练/测试入口：加载 Config → 构建 Runner |
| [train.sh](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/train.sh) | 一行式训练启动脚本（设置 PYTHONPATH 并调 dist_train.sh） |

涉及注册的 `uniseg3d/` 子模块会在 4.2.3 精读中逐一给出。

## 4. 核心概念与源码讲解

### 4.1 目录结构与各目录职责

#### 4.1.1 概念说明

Ground3D-LMM 是一个典型的「OpenMMLab 插件式研究仓库」：它**不 fork 整个 mmdet3d**，而是自带一个 `uniseg3d` 包，把模型、数据变换、损失、指标作为「插件」挂到已安装的 mmdet3d 框架上。这决定了仓库的目录分工：

- `configs/` 只写**声明**（我要什么，用字符串）；
- `uniseg3d/` 只写**实现**（类怎么算，用 PyTorch）；
- `tools/` 写**入口**（怎么启动：加载配置、构建 Runner）；
- 根目录的 shell 脚本是**一键封装**（拼命令行）；
- `docs/` 给人读，`data/` 给机器读。

#### 4.1.2 核心流程

各目录的调用与依赖关系可以画成一张图：

```text
                 docs/INSTALL.md, DATA_*.md        ← 人类阅读
                        │
bash train.sh ──► tools/dist_train.sh ──► tools/train.py   （入口层）
 PYTHONPATH=./          │
                        │ ① Config.fromfile(配置) 解析继承/合并
                        ▼
              configs/ground3dlmm_train.py          （声明层）
                        │  type='Grounded_LMM_main' 等字符串
                        │ ② custom_imports 触发 import
                        ▼
              uniseg3d/ (模型+变换+损失+指标)         （实现层）
                        │ ③ 注册表：字符串 → 类
                        ▼
              Runner.build_model / build_dataloader （框架层：mmdet3d/mmengine）
                        │
                        ▼
              data/ (points/ infos.pkl QA json)      （数据层，用户填充）
```

一句话概括：**配置负责「点名」，注册表负责「查名」，`uniseg3d` 负责「应答」，`tools` 负责「发令」**。

#### 4.1.3 源码精读

**（1）官方口径的目录结构。** README 的「Repository structure」一节给出了骨架图，并标注了关键文件的一句话职责：

[README.md:L368-L400](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L368-L400) —— 仓库结构总览：`configs/`、`data/`（用户填充）、`docs/`、`tools/`、`uniseg3d/`（模型 + 变换 + 损失）、`work_dirs/`（输出，gitignored），以及根目录的 `train.sh` / `eval_ground3d_*.sh` / `environment.yml` / `pyproject.toml`。

对照实际仓库（本讲已逐一核对），各目录的真实内容与职责如下：

| 目录/文件 | 数量 | 职责 | 代表文件 |
|---|---|---|---|
| `configs/` | 9 个 `.py` | 全部配置：1 个训练 + 8 个评估 | `ground3dlmm_train.py`、`ground3dlmm_eval_{scannet,scannetpp}[_3d].py`、`ground3dlmm_eval_{scanrefer,reason3d}[_image].py` |
| `uniseg3d/` | 27 个 `.py` | 全部核心实现（模型/数据/损失/指标/可视化） | 见下表 |
| `tools/` | 16 个文件 | 入口脚本 + 数据转换 + 评估 + 可视化工具 | `train.py`、`test.py`、`create_data.py`、`evaluate_text_metrics.py` |
| `docs/` | 3 个 `.md` | 安装与数据准备文档 | `INSTALL.md`、`DATA_IMAGES.md`、`DATA_SCANREFER_REASON3D.md` |
| `data/` | 少量随仓库文件 | 数据目录（用户填充），但随仓库发布了类别嵌入 `scannet/scannet_cls_embedding.pth` 和 ScanNet 预处理辅助脚本（`load_scannet_data.py`、`meta_data/` 等） | — |
| 根目录 | `train.sh`、`eval_ground3d.sh` + 3 个旧版 wrapper | 一键启动封装 | `train.sh` 一行完成 4 卡训练启动 |
| `work_dirs/` | 无（gitignored） | 训练输出：checkpoint 与日志 | — |

`uniseg3d/` 内部按功能再分组（本讲为后续单元画的地图）：

| 分组 | 文件 | 后续讲义 |
|---|---|---|
| 模型 | `uniseg3d.py`（主入口 `Grounded_LMM_main`）、`spconv_unet.py`（稀疏主干）、`query_decoder.py`（查询解码器 + 语言模型加载）、`point_qwen3_vl.py` / `point_qwen2_5_vl.py`（点云 token 注入的 Qwen 改造）、`data_preprocessor.py` | u3、u4 |
| 数据 | `scannet_dataset.py`（数据集类）、`loading.py` / `transforms_3d.py` / `formatting.py`（管线三段：加载/变换/打包）、`prompt_template.py` | u2 |
| 损失 | `unified_criterion.py`（组合器）、`text_prompt_instance_criterion.py`、`cot_criterion.py`（语言损失）等 7 个 criterion | u5 |
| 指标 | `unified_metric.py`（`Part3dGlamm_SegMetric`）、`instance_seg_eval.py`、`evaluate_semantic_instance.py` | u6 |
| 可视化 | `grounding_viz.py` | u6 |
| 其他 | `structures.py`（数据结构）、`conversation.py`（对话模板）、`mask_matrix_nms.py`、`mask_pool.py` | u5 |

**（2）打包边界：仓库只把 `uniseg3d` 当作 Python 包。**

[pyproject.toml:L58-L62](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/pyproject.toml#L58-L62) —— `packages = ["uniseg3d"]`：`pip install -e .`（见 [README.md:L45-L50](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L45-L50)）之后，`import uniseg3d` 在任何目录都能成功。`tools/`、`configs/` **不**是包，它们靠路径方式被找到（见 4.2.3 末尾）。

[pyproject.toml:L33-L43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/pyproject.toml#L33-L43) —— 注释明确说明：torch/spconv/mmcv 等重型 CUDA 依赖**不**列在 `install_requires` 里（要按 u1-l2 的流程对齐 CUDA 安装），这里只管纯 Python 的小依赖。这是「研究仓库打包」的典型务实做法。

**（3）入口与启动脚本的层次。**

[train.sh:L1](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/train.sh#L1) —— 整个「训练启动器」只有一行：设置 `CUDA_VISIBLE_DEVICES`、`PYTHONPATH=./`，然后调 `tools/dist_train.sh` 起 4 卡。README 还说明了三个 `eval_ground3d_{scannet,scannetpp,all}.sh` 只是统一脚本 `eval_ground3d.sh` 的薄封装（[README.md:L262-L264](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L262-L264)）。

#### 4.1.4 代码实践

**实践目标**：不动任何代码，用 `git ls-files` 把「目录职责」从口头结论变成亲眼验证。

**操作步骤**：

```bash
cd Ground3D-LMM   # 仓库根目录

# 1. 看每个关键目录下被 git 跟踪的文件
git ls-files configs
git ls-files tools
git ls-files uniseg3d
git ls-files docs

# 2. 数一下每个目录的文件数
for d in configs tools uniseg3d docs; do
  echo "$d: $(git ls-files "$d" | wc -l)"
done

# 3. 找出根目录的所有启动脚本和「入口特征」
git ls-files '*.sh'
grep -l "Runner" tools/*.py     # 哪些工具真正构建了 Runner
```

**需要观察的现象**：

- `configs` 恰好 9 个文件，文件名呈现清晰规律：`ground3dlmm_train.py`、`ground3dlmm_eval_<数据集>[_3d].py`、`ground3dlmm_eval_<基准>[_image].py`；
- `uniseg3d` 恰好 27 个 `.py`；`tools` 16 个；`docs` 3 个；
- `grep -l "Runner" tools/*.py` 只应命中 `train.py` 和 `test.py`——说明**只有这两个是框架入口**，其余 `tools/` 脚本是数据/评估/可视化工具；
- `work_dirs/` 不会出现在 `git ls-files` 输出里（被 gitignore）。

**预期结果**：上面四个观察点全部命中。本实践只读 git 元数据，任何环境都能跑（本讲写作时已实际核对目录清单）。第 2 步的循环命令如在你的 shell 不可用，逐个执行 `git ls-files <目录> | wc -l` 即可。

#### 4.1.5 小练习与答案

**练习 1**（判断）：你想把解码器从 6 层改成 8 层、LoRA rank 从 16 改成 32，应该改 `configs/` 还是 `uniseg3d/`？

> **答案**：改 `configs/`。层数和 rank 都是构造参数（`ground3dlmm_train.py` 里 `decoder` 的 `num_layers=6`、`lora_r=16`），属于「声明」；只有当算法逻辑本身变化（比如换一种注意力实现）才需要动 `uniseg3d/`。

**练习 2**：`bash train.sh` 和 `python tools/train.py` 之间是什么关系？

> **答案**：`train.sh` 是封装层——设置 GPU 编号和 `PYTHONPATH`，调用 `tools/dist_train.sh`，后者最终用 `torch.distributed.launch` 在 4 张卡上各启一个 `tools/train.py` 进程。真正加载配置、构建 Runner 的入口是 `tools/train.py`。

**练习 3**：为什么 `data/` 目录里会随仓库带一个 `scannet_cls_embedding.pth`，而不让用户自己下载？

> **答案**：它是训练配置中 `decoder` 的 `vocabulary_cls_embedding_path='data/scannet/scannet_cls_embedding.pth'`（见 [configs/ground3dlmm_train.py:L66](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L66)）所需的类别文本嵌入，体积小、可再分发，所以直接进仓库；而 ScanNet 点云数据不可再分发，只能用户自己准备。

### 4.2 mmdet3d 注册表与 custom_imports

#### 4.2.1 概念说明

mmdet3d 从 `mmdet3d.registry` 导出若干**根注册表**：`MODELS`（模型/损失/数据预处理器）、`DATASETS`（数据集）、`TRANSFORMS`（数据管线变换）、`METRICS`（评估指标）、`TASK_UTILS`（匹配器、代价函数等）。它们都是 mmengine 全局注册表的子注册表。

本仓库的策略非常直接：**所有自定义类都用装饰器注册进 mmdet3d 的注册表**，名字就沿用类名。这样配置里 `type='xxx'` 的解析完全交给框架。

但有一个冷启动问题：注册发生在 **import `uniseg3d` 包的时候**，而 `tools/train.py` 只 import 了 mmengine/mmdet3d，没人 import 你的插件包。`custom_imports` 就是 OpenMMLab 官方提供的解法——**在配置文件里声明「请先 import 这些模块」**，框架会在构建 Runner 时替你执行这次 import。

#### 4.2.2 核心流程

从 `bash train.sh` 到模型实例化的完整链条：

```text
1. train.sh ──► tools/dist_train.sh ──► python tools/train.py configs/ground3dlmm_train.py
2. tools/train.py:  cfg = Config.fromfile(args.config)        # 解析 _base_ 继承 + 合并
3. tools/train.py:  runner = Runner.from_cfg(cfg)
4. Runner 初始化时读取 cfg.custom_imports
   ──► import uniseg3d
5. uniseg3d/__init__.py 的 19 行 import 级联执行各子模块
   ──► 每个模块顶层的 @MODELS/@DATASETS/@TRANSFORMS/...register_module() 逐个生效
6. Runner 构建模型：MODELS.build(dict(type='Grounded_LMM_main', ...))
   ──► 注册表按名字查到类 ──► 递归构建子字典（backbone/decoder/criterion 也是 dict(type=...)）
7. 同样地：DATASETS.build / TRANSFORMS.build 构建 dataloader 与数据管线
```

用伪代码表示注册副作用：

```python
# 装饰器等价于：
class Grounded_LMM_main: ...
MODELS.module_dict['Grounded_LMM_main'] = Grounded_LMM_main   # ← import 时自动发生
```

#### 4.2.3 源码精读

**（1）配置里的那一行开关。**

[configs/ground3dlmm_train.py:L1-L6](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L1-L6) —— 训练配置开头：`_base_` 声明两个基配置（4.3 节展开），第 5 行 `custom_imports = dict(imports=['uniseg3d'])` 就是插件接入点。评估配置（如 [configs/ground3dlmm_eval_ground3d_scannet.py:L4](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L4)）也都有同一行——**每个配置文件都要自带这行**，因为它就是「本配置依赖 uniseg3d 包」的显式声明。

`custom_imports` 的处理是 mmengine 框架内置行为（不在本仓库源码内）：Runner 初始化时会调用 `mmengine.registry.import_modules_from_strings`，逐个 `importlib.import_module`；这也意味着 import 失败会立刻报错（`allow_failed_imports` 默认 False），不会静默。

**（2）包入口：19 行 import 的级联。**

[uniseg3d/\_\_init\_\_.py:L1-L19](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/__init__.py#L1-L19) —— 整个包入口只有 19 行 `from .xxx import ...`。注意它**一个注册动作都没有**，注册全部发生在被导入子模块的顶层。这 19 行覆盖了含注册代码的全部 16 个子模块（`prompt_template.py`、`conversation.py`、`grounding_viz.py` 等无注册的模块由需要它们的模块间接导入，如 `query_decoder.py` 导入 `prompt_template`）。

**（3）训练配置中每个 `type` 字符串的注册出处。** 这是本讲的核心对照表（行号均已核对）：

| 配置中的 `type` | 配置位置 | 注册处（类定义） | 注册表 |
|---|---|---|---|
| `Grounded_LMM_main` | [ground3dlmm_train.py:L29](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L29) | [uniseg3d/uniseg3d.py:L905-L906](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L905-L906) | MODELS |
| `Det3DDataPreprocessor_` | L30 | [uniseg3d/data_preprocessor.py:L7-L8](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/data_preprocessor.py#L7-L8) | MODELS |
| `SpConvUNet`（backbone） | L44 | [uniseg3d/spconv_unet.py:L94-L95](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/spconv_unet.py#L94-L95) | MODELS |
| `Grounded_Decoder_Joint`（decoder） | L48 | [uniseg3d/query_decoder.py:L880-L881](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L880-L881) | MODELS |
| `Part3dGlammCriterion_v2`（criterion） | L80 | [uniseg3d/unified_criterion.py:L126-L127](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_criterion.py#L126-L127) | MODELS |
| `TextPrompt_Criterion_Glamm`（分割损失） | L82 | [uniseg3d/text_prompt_instance_criterion.py:L750-L751](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L750-L751) | MODELS |
| `CoTCriterion`（语言损失） | L92 | [uniseg3d/cot_criterion.py:L7-L8](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/cot_criterion.py#L7-L8) | MODELS |
| `Part3dGlamm_Dataset`（数据集） | L97、L170/L180 | [uniseg3d/scannet_dataset.py:L40-L41](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/scannet_dataset.py#L40-L41) | DATASETS |
| `LoadAnnotations3D_` | L115 | [uniseg3d/loading.py:L10-L11](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L10-L11) | TRANSFORMS |
| `NormalizePointsColor_` | L129 | [uniseg3d/loading.py:L156-L157](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/loading.py#L156-L157) | TRANSFORMS |
| `AddSuperPointAnnotations_Part3DGlamm` | L132 | [uniseg3d/transforms_3d.py:L1264-L1265](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1264-L1265) | TRANSFORMS |
| `QA_Generation_v2` | L138 | [uniseg3d/transforms_3d.py:L416-L417](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L416-L417) | TRANSFORMS |
| `ElasticTransfrom` | L145 | [uniseg3d/transforms_3d.py:L1051-L1052](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1051-L1052) | TRANSFORMS |
| `Pack3DDetInputs_Part_Glamm` | L151 | [uniseg3d/formatting.py:L159-L160](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/formatting.py#L159-L160) | TRANSFORMS |
| `Part3dGlamm_SegMetric`（评估） | [eval 配置 L242-L244](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L242-L244) | [uniseg3d/unified_metric.py:L366-L367](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/unified_metric.py#L366-L367) | METRICS |

表中未列的 `LoadPointsFromFile`、`GlobalRotScaleTrans`、`MultiScaleFlipAug3D`、`ConcatDataset`、`OptimWrapper`、`AdamW`、`EpochBasedTrainLoop`、`EmptyCacheHook`、`PolyLR` 等，都是 mmdet3d / mmengine **上游自带的已注册类**，无需本仓库注册。

看一个具体注册点的样子（其余全部同构）：

```python
@MODELS.register_module()                     # ← import 时把类名写入 MODELS 注册表
class Grounded_LMM_main(UniSeg3DMixin, Base3DDetector):
    ...
```

（出处即上表 [uniseg3d.py:L905-L906](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L905-L906)；`MODELS` 来自该文件顶部 `from mmdet3d.registry import MODELS`。）

三个值得注意的细节：

1. **注册名 = 类名**。`register_module()` 不传参数时默认用类名做键。本仓库大量使用「原名 + 下划线后缀」的命名（`LoadAnnotations3D_`、`Pack3DDetInputs_`、`Det3DDataPreprocessor_`）表示「继承 mmdet3d 同名类并小改」的版本——所以配置字符串必须**连同下划线**一字不差。
2. **import 副作用是真实的，哪怕是无效语句。** [uniseg3d/uniseg3d.py:L1](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1) 首行是一句误触 IDE 自动补全留下的 `from turtle import pd`（标准库 turtle 的一个别名）——它毫无用处，但每次 `import uniseg3d` 都会执行。研究代码的常态，读源码时不必困惑。
3. **让 `import uniseg3d` 成功的三条路径**（任一即可，仓库三条全备）：
   - `pip install -e .`（[pyproject.toml:L58-L62](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/pyproject.toml#L58-L62) 打包 `uniseg3d`）；
   - 启动脚本设 `PYTHONPATH=./`（[train.sh:L1](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/train.sh#L1)）；
   - 入口脚本自己追加路径：`sys.path.append(os.path.dirname(__file__) + '/..')`（[tools/train.py:L6-L7](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L6-L7)、[tools/test.py:L6](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/test.py#L6)）。

**（4）`type` 解析的终点：Runner 构建。**

[tools/train.py:L69](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L69) 与 [tools/train.py:L124-L126](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L124-L126) —— 先 `Config.fromfile`，再 `Runner.from_cfg(cfg)`。Runner 会按配置里的 `default_scope`（由基配置 `mmdet3d::_base_/default_runtime.py` 设为 `'mmdet3d'`，该文件在已安装的 mmdet3d 包内、不在本仓库）在 mmdet3d 作用域下查注册表；查不到时（如 `AdamW`）沿作用域链回退到 mmengine 根注册表。`model`、`backbone`、`decoder`、`criterion`、数据集、pipeline 每个 `dict(type=...)` 都会被递归地 `build`——这就是「配置字符串变成对象」的全过程。

#### 4.2.4 代码实践

**实践目标**：亲手触发并观察「import 副作用注册」——不构建任何模型，只验证注册表里多了什么。

**操作步骤**（在完成 u1-l2 安装的 conda 环境中，仓库根目录下）：

1. 新建脚本 `check_registry.py`（示例代码，放在仓库外的任意目录均可，或临时目录）：

```python
# check_registry.py —— 示例代码：验证 uniseg3d 的注册副作用
from mmengine.registry import import_modules_from_strings

import_modules_from_strings(imports=['uniseg3d'])   # 等价于配置里的 custom_imports

from mmdet3d.registry import MODELS, DATASETS, TRANSFORMS, METRICS

pairs = [
    (MODELS,     'Grounded_LMM_main'),
    (MODELS,     'Grounded_Decoder_Joint'),
    (DATASETS,   'Part3dGlamm_Dataset'),
    (TRANSFORMS, 'QA_Generation_v2'),
    (METRICS,    'Part3dGlamm_SegMetric'),
]
for reg, name in pairs:
    cls = reg.module_dict.get(name)
    print(f'{name:32s} -> {cls}')

print('\nMODELS 中以 Grounded 开头：',
      sorted(k for k in MODELS.module_dict if k.startswith('Grounded')))
print('TRANSFORMS 中含 Glamm：',
      sorted(k for k in TRANSFORMS.module_dict if 'Glamm' in k))
```

2. 运行：`python check_registry.py`
3. 对比实验：把第一行 `import_modules_from_strings(...)` 注释掉再运行一次，观察差异。

**需要观察的现象**：

- 第一次运行：5 个类全部解析成功（打印出 `<class 'uniseg3d.xxx.Yyy'>`），且两个列表非空——`Grounded` 开头的应包含 `Grounded_LMM_main`、`Grounded_Decoder_Joint`、`Grounded_Decoder_Eval` 等；`Glamm` 列表应包含 `AddSuperPointAnnotations_Part3DGlamm` 和 `Pack3DDetInputs_Part_Glamm`；
- 注释后：除非当前 Python 进程曾经以其他方式 import 过 `uniseg3d`，否则查询结果为 `None`、列表为空——直观证明「没有 import 就没有注册」。

**预期结果**：如上。注意首次运行会真实 import torch / spconv / MinkowskiEngine / transformers 等重型依赖（这正是 u1-l2 环境的价值），耗时数十秒属正常；不需要 GPU，不需要下载数据和权重。**运行输出待本地验证**（本讲义写作环境未装完整训练环境，脚本中 API 名称已对照 mmengine 文档与仓库用法核对）。

#### 4.2.5 小练习与答案

**练习 1**：如果把训练配置第 5 行 `custom_imports` 删掉再启动训练，会发生什么？

> **答案**：Runner 构建 `MODELS.build(dict(type='Grounded_LMM_main', ...))` 时，注册表里根本没有这个键（没有任何代码 import 过 `uniseg3d`），mmengine 会抛出注册表查找错误（形如 "Grounded_LMM_main is not in the MODELS registry"）。这就是每个配置文件都要带这行的原因。

**练习 2**：`__init__.py` 里写的是 `from .uniseg3d import UniSeg3D`，可它为什么能让**别的类**（`Grounded_LMM_main`）也完成注册？

> **答案**：import 语句会执行整个 `uniseg3d/uniseg3d.py` 模块的顶层代码，包括第 905 行的 `@MODELS.register_module()` 装饰器；`from x import y` 导入哪个名字不重要，重要的是模块 `x` 被完整执行了一次。

**练习 3**：本仓库为什么把 `LoadAnnotations3D_` 这种类名带上尾下划线，而不是直接覆盖 mmdet3d 原生的 `LoadAnnotations3D`？

> **答案**：注册键默认等于类名，加上划线是「另注册一个改版类、不动上游原类」的最低成本做法——原生 `LoadAnnotations3D` 继续可用，两个版本在注册表里共存，配置里用哪个写哪个。

### 4.3 mmengine Config 继承与覆盖

#### 4.3.1 概念说明

配置文件之间不是孤立的。mmengine Config 提供一套「面向对象的配置」机制：

- **`_base_` 继承**：子配置自动获得基配置的全部字段；
- **递归覆盖**：子配置里重写某个 dict 的字段，只覆盖写的那些键，其余保留；
- **`_delete_=True`**：某层子 dict 想完全丢弃基配置对应内容、重新来过时使用；
- **跨包语法 `包名::路径`**：基配置可以来自**已安装的包**（如 mmdet3d），而不只是本地文件。

这套机制让 Ground3D-LMM 用「只覆盖 3 个字段」就派生出 3D-only 变体的完整配置。

#### 4.3.2 核心流程

`Config.fromfile()` 的合并规则（自基配置向子配置递归）：

| 情形 | 规则 | 本仓库示例 |
|---|---|---|
| dict vs dict | 递归合并：同名键被子配置覆盖，**新增键保留、未提及键继承** | `_3d` 配置只改 `decoder.lora_r` 等 3 个键，`qwen_model_path` 等全部继承 |
| list | **整体替换**，不做逐元素合并 | `_3d` 若写 `keys=[...]` 会替换整份打包键列表（所以它没写） |
| `_delete_=True` | 先丢弃基配置中该子 dict 的全部内容，再按子配置重建 | 训练配置替换 `train_dataloader.dataset` |
| `包名::路径` | 到已安装的包内寻找基配置 | `mmdet3d::_base_/datasets/scannet-seg.py` |
| 显式 `= None` | 子配置把某字段设为 None，表示「取消基配置的这一项」 | `val_dataloader = None` |

运行期还有第四层覆盖：`--cfg-options`（由 [tools/train.py:L76-L77](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/train.py#L76-L77) 的 `cfg.merge_from_dict(args.cfg_options)` 消费）按**点路径**写值，路径中的数字表示列表下标。

#### 4.3.3 源码精读

**（1）跨包继承 + 插件声明。**

[configs/ground3dlmm_train.py:L1-L6](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L1-L6) —— `_base_` 列了两个基配置：`mmdet3d::_base_/default_runtime.py`（默认运行时：日志、hook、`default_scope='mmdet3d'`）和 `mmdet3d::_base_/datasets/scannet-seg.py`（ScanNet 分割的 dataloader/evaluator 模板）。两者都在**已安装的 mmdet3d 包**里，`::` 语法让配置可以「借」上游库的模板而不必拷贝进仓库。

**（2）`_delete_`：换掉基配置的数据集。**

[configs/ground3dlmm_train.py:L162-L189](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L162-L189) —— 训练数据加载器：外层 `train_dataloader` 覆盖 `batch_size=1`、`num_workers=16`；内层 `dataset` 带 `_delete_=True`（第 166 行），把基配置里 `ScanNetSegDataset` 那套数据集声明**整体丢弃**，换成 `ConcatDataset` 包着两个 `Part3dGlamm_Dataset`（ScanNet + ScanNet++，第 168-188 行）。如果不写 `_delete_`，合并会把基配置残留的旧键（如上游的 pipeline、ann_file）混进来，产生难以排查的配置污染。

**（3）显式断开继承。**

[configs/ground3dlmm_train.py:L191-L195](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L191-L195) —— 训练不需要验证/测试流程，于是把 `val_dataloader`、`val_evaluator`、`test_dataloader`、`test_evaluator` 显式设为 `None`，并留注释说明意图：基配置 scannet-seg.py 预置了这些组件，不置 None 就会被继承下来。这是「继承是把双刃剑」的最好注脚——**不想要的东西要主动关掉**。

**（4）极简派生：3 个字段得到一个新变体。**

[configs/ground3dlmm_eval_ground3d_scannet_3d.py:L1-L27](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet_3d.py#L1-L27) —— 3D-only 变体的评估配置**全文只有 27 行**：`_base_` 指向 Joint 配置，然后只覆盖 `decoder` 的 `lora_r=32`、`lora_alpha=64`、`image_dir=None` 三个字段。文件顶部的 docstring 还解释了为什么必须覆盖：把 r=32 的 checkpoint 权重加载进 r=16 的模型时，不匹配的 adapter 会被 `strict=False` **静默忽略**，模型悄悄退回裸 Qwen——这是 u1-l1 提过的坑在配置层面的解法。其余一切（模型结构、pipeline、指标）原样继承。

**（5）运行期覆盖：点路径穿透列表。**

[README.md:L104-L112](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L104-L112) —— 快速上手命令用 `--cfg-options "test_dataloader.dataset.pipeline.2.transforms.2.sub_task=scale_estimation"` 切换子任务。对照 eval 配置可精确解码这条路径：`test_dataloader.dataset.pipeline` 即 `test_pipeline` 列表，`[2]` 是 [configs/ground3dlmm_eval_ground3d_scannet.py:L181-L201](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L181-L201) 的 `MultiScaleFlipAug3D`，其内部 `transforms` 列表的 `[2]` 是 `QA_Generation_Test`（第 195-198 行），最终改的是它的 `sub_task` 字段——**点路径里的数字就是列表下标**。

**（6）配置即代码的小例子。**

[configs/ground3dlmm_train.py:L43-L46](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L43-L46) —— 主干通道数用列表推导生成 `[32, 64, 96, 128, 160]`；同文件 [L19-L23](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L19-L23) 定义 `class_names` 列表后在 `metainfo`、`target_classes` 两处复用。JSON/YAML 做不到这一点，这是配置选 `.py` 的根本原因。

#### 4.3.4 代码实践

**实践目标**：用 `Config.fromfile` 观察继承与覆盖的实际效果，验证「27 行文件 = 完整配置」。

**操作步骤**（在 u1-l2 环境中，仓库根目录下；无需 GPU/数据）：

1. 新建脚本 `check_config.py`（示例代码）：

```python
# check_config.py —— 示例代码：观察 Config 继承与覆盖
from mmengine.config import Config

joint = Config.fromfile('configs/ground3dlmm_eval_ground3d_scannet.py')
d3    = Config.fromfile('configs/ground3dlmm_eval_ground3d_scannet_3d.py')

print('model.type              :', d3.model.type)                    # 继承得到的顶层字段
print('decoder.lora_r          :', joint.model.decoder.lora_r, '->', d3.model.decoder.lora_r)
print('decoder.lora_alpha      :', joint.model.decoder.lora_alpha, '->', d3.model.decoder.lora_alpha)
print('decoder.image_dir       :', joint.model.decoder.image_dir, '->', d3.model.decoder.image_dir)
print('decoder.qwen_model_path :', d3.model.decoder.qwen_model_path)  # 未覆盖，应仍存在
print('decoder.num_layers      :', d3.model.decoder.num_layers)       # 未覆盖，应仍为 6

train = Config.fromfile('configs/ground3dlmm_train.py')
print('\ntrain dataset.type       :', train.train_dataloader.dataset.type)   # 应为 ConcatDataset
print('子数据集个数             :', len(train.train_dataloader.dataset.datasets))
print('val_dataloader           :', train.val_dataloader)                   # 应为 None
```

2. 运行 `python check_config.py`。
3. 思考实验（不改代码）：如果 `_3d` 配置里写的是 `_delete_=True` 再加 `lora_r=32`，`qwen_model_path` 还会存在吗？

**需要观察的现象**：

- `d3.model.type` 打印 `Grounded_LMM_main`——子配置没写，纯靠两层继承（`_3d` → joint → `mmdet3d::` 基配置链）；
- `lora_r` 从 16 变 32、`lora_alpha` 从 32 变 64、`image_dir` 从路径变 `None`——三处覆盖；
- `qwen_model_path`、`num_layers` 原样保留——dict 递归合并不动未提及的键；
- 训练配置的 dataset 是 `ConcatDataset` 且有 2 个子数据集；`val_dataloader` 为 `None`。

**预期结果**：如上各行。**运行输出待本地验证**（写作环境无 mmengine/mmdet3d；字段名与默认值已直接对照仓库配置源码核对：`lora_r`/`lora_alpha`/`qwen_model_path`/`num_layers=6` 见训练配置 L48-L75，`image_dir` 见评估配置 L74）。第 3 步的答案：`_delete_=True` 会把基配置 `decoder` 整个子字典丢弃，只剩你显式写的键——`qwen_model_path` 等会**全部丢失**，构建时会用类的默认参数，可能引发完全不同的行为。这正是该仓库「只用覆盖、不用 `_delete_`」派生 `_3d` 配置的原因。

#### 4.3.5 小练习与答案

**练习 1**：`_delete_=True` 写在 `train_dataloader.dataset` 这一层，删除的范围到底是什么？

> **答案**：只删除基配置中 `train_dataloader.dataset` 这个**子字典**的全部内容（上游的 ScanNetSegDataset 声明），`train_dataloader` 其余字段（如上游的 batch_size、num_workers）仍走普通合并——所以本配置又显式写了 `batch_size=1`、`num_workers=16` 来确定取值。`_delete_` 是字典级的，不会波及兄弟层级。

**练习 2**：训练配置为什么要把 `val_dataloader` 等四个字段显式设为 `None`，而不是删掉不写？

> **答案**：不写就「继承」。基配置 `mmdet3d::_base_/datasets/scannet-seg.py` 预置了 ScanNet 验证/测试的 dataloader 和 SegMetric evaluator，删掉不写它们会全部被继承下来，Runner 会试图构建一套与本任务不匹配的验证流程。设 `None` 是显式声明「取消这一项」，配置 L191 的注释写明了这个意图。

**练习 3**：`--cfg-options "test_dataloader.dataset.pipeline.2.transforms.2.sub_task=scale_estimation"` 这条点路径里两个 `.2` 分别指什么？

> **答案**：`merge_from_dict` 的点路径遇到列表时把数字当下标。第一个 `2` 指 `test_pipeline` 的第 3 个变换 `MultiScaleFlipAug3D`（eval 配置 L181），第二个 `2` 指它内部 `transforms` 列表的第 3 个变换 `QA_Generation_Test`（L195）。整条路径最终改的是 `QA_Generation_Test.sub_task`——不修改任何文件就切换了评估子任务。

## 5. 综合实践

**任务：写一个「配置 → 注册表」全链路追踪器。** 把本讲三个模块串起来：加载训练配置，递归遍历 `model` 与 `train_dataloader`，把每个 `type` 字符串解析成「注册表 + 真实类」，输出一张对照表——这就是 4.2.3 那张核心表格的自动化版本，也是你今后读任何 OpenMMLab 配置的通用工具。

**操作步骤**（在 u1-l2 环境中，仓库根目录下）：

```python
# trace_config.py —— 示例代码：把配置里每个 type 字符串解析成真实类
from mmengine.registry import import_modules_from_strings
import_modules_from_strings(imports=['uniseg3d'])      # ① 模块 4.2：先触发注册

from mmengine.config import Config                      # ② 模块 4.3：再加载合并后的配置
from mmdet3d.registry import MODELS, DATASETS, TRANSFORMS, METRICS

REGS = [MODELS, DATASETS, TRANSFORMS, METRICS]

def resolve(name):
    for reg in REGS:
        if name in reg.module_dict:                     # 只查 mmdet3d 子注册表
            return f'{reg.name}:{reg.module_dict[name].__module__}.{name}'
    return '(上游 mmengine/mmdet3d 内置，或未注册)'      # 例如 AdamW、ConcatDataset

def walk(node, path):
    if isinstance(node, dict):
        t = node.get('type')
        if isinstance(t, str):
            print(f'{path:<48} {t:<36} -> {resolve(t)}')  # ③ 模块 4.1：目录即职责
        for k, v in node.items():
            if k != 'type':
                walk(v, f'{path}.{k}')
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            walk(v, f'{path}[{i}]')

cfg = Config.fromfile('configs/ground3dlmm_train.py')
walk(cfg.model.to_dict(), 'model')
walk(cfg.train_dataloader.to_dict(), 'train_dataloader')
```

**需要观察的现象**（输出示意，实际以运行为准）：

```text
model                                Grounded_LMM_main      -> models:uniseg3d.uniseg3d.Grounded_LMM_main
model.backbone                       SpConvUNet             -> models:uniseg3d.spconv_unet.SpConvUNet
model.decoder                        Grounded_Decoder_Joint -> models:uniseg3d.query_decoder.Grounded_Decoder_Joint
model.criterion                      Part3dGlammCriterion_v2 -> models:uniseg3d.unified_criterion.Part3dGlammCriterion_v2
model.criterion.lang_seg_criterion   TextPrompt_Criterion_Glamm -> ...
model.criterion.lang_criterion       CoTCriterion           -> ...
train_dataloader.dataset             ConcatDataset          -> (上游 mmengine/mmdet3d 内置，或未注册)
train_dataloader.dataset.datasets[0] Part3dGlamm_Dataset    -> datasets:uniseg3d.scannet_dataset.Part3dGlamm_Dataset
train_dataloader.dataset.datasets[0].pipeline[0] LoadPointsFromFile -> (上游...)
train_dataloader.dataset.datasets[0].pipeline[1] LoadAnnotations3D_  -> transforms:uniseg3d.loading.LoadAnnotations3D_
...
```

**预期结果**：

1. `model` 一支全部解析到 `uniseg3d.*`，与 4.2.3 表格逐行吻合；
2. `ConcatDataset`、`LoadPointsFromFile`、`GlobalRotScaleTrans` 等显示为上游内置——它们注册在 mmengine 根注册表或 mmdet3d 原生模块里，本追踪器只查四个 mmdet3d 子注册表所以标为「上游」，这正好演示了 `default_scope` 作用域链的存在（框架 build 时会自动跨作用域查找）；
3. `pipeline` 按索引展开，能直观看到 [训练配置 L106-L158](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L106-L158) 的 8 个变换依次是：加载点云 → 加载标注 → 旋转缩放平移增强 → 颜色归一化 → 超点标注 → QA 采样 → 弹性形变 → 打包——这张顺序表就是 u2 单元（数据管线）的预习提纲。

**待本地验证**：脚本依赖 u1-l2 环境中的 mmengine/mmdet3d；API（`import_modules_from_strings`、`Registry.module_dict`、`ConfigDict.to_dict`）按 mmengine 公开接口书写，输出示意中的注册表前缀（`models:`/`datasets:`/`transforms:`）以实际运行打印为准。

## 6. 本讲小结

- 仓库五层分工：`configs/` 声明（字符串点名）、`uniseg3d/` 实现（27 个文件覆盖模型/数据/损失/指标）、`tools/` 入口（只有 `train.py`/`test.py` 构建 Runner）、根目录 shell 一键封装、`data/` 由用户填充（仅类别嵌入等小资产随仓库分发）。
- `custom_imports = dict(imports=['uniseg3d'])` 触发包入口 19 行 import，级联执行各模块顶层的 `@XXX.register_module()` 装饰器——「import 的副作用」让整个包一次性挂载进 mmdet3d 注册表。
- 配置里的 `type='Grounded_LMM_main'` 等字符串在 Runner 构建时按「default_scope='mmdet3d' → 递归 build 子 dict」解析成真实类；本讲给出训练配置全部自定义 type 与注册出处的对照表。
- mmengine Config 三件套：`_base_` 跨包继承（`mmdet3d::` 语法）、dict 递归覆盖（未提及键保留）、`_delete_=True` 字典级重建；`_3d` 变体只覆盖 `lora_r`/`lora_alpha`/`image_dir` 三个字段即成完整配置。
- 运行期 `--cfg-options` 用点路径覆盖，数字即列表下标（`pipeline.2.transforms.2.sub_task=...` 直达 `QA_Generation_Test`）。
- 警示两点：注册名默认等于类名（尾下划线是本仓库「改名继承」的惯例，type 字符串必须一字不差）；`_delete_` 与 LoRA rank 不匹配的静默失载一样，都是「配置写错不报错、行为悄悄变」的高危点。

## 7. 下一步学习建议

- **下一讲 u1-l4（快速上手推理）** 会把本讲的机制跑起来：用 `--cfg-options` 实际切换 `task_level`/`sub_task` 完成一次 `scale_estimation` 推理——本讲 4.3 的点路径知识将直接派上用场。
- 建议现在回头重读一遍 [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py)：此刻你应该能对配置里**每一个** `type` 说出它的注册文件，对每一个 `_base_`/`_delete_` 说出它的合并后果。
- 好奇 mmengine 细节的读者，可在本地环境中打开 `site-packages/mmengine/registry/registry.py`（`register_module`、`build`、作用域切换）和 `site-packages/mmdet3d/_base_/default_runtime.py`（`default_scope='mmdet3d'` 的出处），与本讲对照。
- u2 单元将沿着本讲 4.2.3 表格中 DATASETS/TRANSFORMS 两列向下钻：`Part3dGlamm_Dataset` 怎么读 infos.pkl、`QA_Generation_v2` 怎么采样问答对、`Pack3DDetInputs_Part_Glamm` 打包了哪些键。
