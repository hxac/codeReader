# u1-l2 仓库结构与四大子项目地图

## 1. 本讲目标

上一讲（u1-l1）我们确立了全手册最重要的事实：**Qwen3-VL 仓库只包含工程代码，模型结构实现位于 transformers 库中，权重托管在 Hugging Face / ModelScope 上**。本讲我们要把镜头拉近，读懂这个仓库本身的组织方式。学完本讲，你应该能够：

1. 说出仓库四大子项目（qwen-vl-utils、qwen-vl-finetune、evaluation、cookbooks）各自的**职责边界**——它们分别解决"输入预处理、微调、评测、能力示例"四类问题。
2. 为每个子项目找到**入口文件或说明文档**，知道从哪个文件开始读。
3. 面对"我想改输入分辨率 / 我想微调 / 我想加一个评测集 / 我想学 Agent 用法"这类新需求时，**准确判断应该动哪个子项目**，而不是在仓库里盲目翻找。

## 2. 前置知识

本讲是纯"地图课"，不需要数学或深度学习基础，但建议你先理解以下几个工程概念：

- **子项目（subproject）**：一个大仓库里可以同时装几个互相独立的小项目。本仓库就是这种"多合一"结构——四个子项目之间没有 import 依赖（只有 qwen-vl-utils 会被根目录脚本和评测脚本当作 pip 包引用），各自有独立的 README、依赖和用途。
- **pip 可编辑安装（`pip install -e <目录>`）**：把一个本地目录以"源码即包"的方式装进 Python 环境，改源码立即生效，无需重新安装。判断一个目录是不是独立 Python 包，看它有没有 `pyproject.toml`。
- **入口文件（entry point）**：运行一个项目时第一个被执行的文件。对命令行工具来说是 `main` 脚本，对库来说是被外部 import 的那个模块。
- **README 是地图的地图**：每个子项目的 README 是官方给的第一手导读，本讲会大量引用它们的原文位置。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | 总仓库说明：模型介绍、Cookbooks 清单、Quickstart、部署指南 |
| [qwen-vl-utils/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/README.md) | 预处理工具库用法：三代模型（Qwen2VL/2.5VL/3VL）的消息写法对照 |
| [qwen-vl-utils/pyproject.toml](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/pyproject.toml) | qwen-vl-utils 的包定义：名称、版本、依赖、构建方式 |
| [qwen-vl-utils/src/qwen_vl_utils/__init__.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/__init__.py) | 库的对外出口：只导出 5 个函数 |
| [qwen-vl-utils/src/qwen_vl_utils/vision_process.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py) | 全部视觉预处理逻辑（533 行）：smart_resize、fetch_image、fetch_video 等 |
| [qwen-vl-finetune/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md) | 微调框架说明：目录结构、依赖版本、数据格式、数据集注册 |
| [web_demo_mm.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py) | 根目录的 Web 演示入口（下一讲 u1-l4 精读） |
| [docker/](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker) | Docker 部署辅助文件 |

## 4. 核心概念与源码讲解

本讲的四个最小模块对应仓库的四个层次：**根目录（门面）→ qwen-vl-utils（输入侧）→ qwen-vl-finetune（训练侧）→ evaluation + cookbooks（应用侧）**。这四个模块合起来正好覆盖模型使用的完整生命周期：输入 → 推理 → 微调 → 评测 → 场景化应用。

### 4.1 根目录文件

#### 4.1.1 概念说明

把仓库 clone 下来后，第一眼看到的文件决定了这个仓库的"门面"。本仓库的根目录**没有 setup.py、没有 pyproject.toml**——这是一个明确信号：根目录本身不是一个 Python 包，而是一个"工程集合"。根目录只放了两类东西：

1. **全局说明与许可**：`README.md`、`LICENSE`。
2. **开箱即用的演示**：`web_demo_mm.py`（Gradio 网页演示脚本）+ `requirements_web_demo.txt`（它的依赖清单）+ `docker/`（容器化启动）。

#### 4.1.2 核心流程

一个新用户接触本仓库的典型路径：

```text
读 README.md（了解模型能力）
    ↓
按 Quickstart 装 transformers>=4.57.0，跑通最小推理
    ↓
想有界面？→ pip install -r requirements_web_demo.txt → python web_demo_mm.py
    ↓
想容器化？→ 用 docker/ 里的 Dockerfile 和启动脚本
    ↓
想深入某类能力？→ 进入四大子项目之一
```

#### 4.1.3 源码精读

先看根 README 的定位。开头一屏是模型简介与全系列链接，其中第 109-126 行的 Cookbooks 表格是"能力 → 子项目"的第一份索引：

- [README.md:L109-L126](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L109-L126)：官方 Cookbooks 清单，把 12 项能力（全能识别、文档解析、2D/3D 定位、OCR、视频理解、手机/电脑 Agent、多模态编码等）逐一映射到 `cookbooks/` 下的笔记本文件。这张表就是 cookbooks 子项目的"目录"。

- [README.md:L128-L135](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L128-L135)：Quickstart 开头，明确写出唯一的推理侧硬性依赖：`pip install "transformers>=4.57.0"`。再次印证 u1-l1 的结论——模型代码在 transformers 里，不在本仓库。

根目录唯一的应用脚本是 web 演示。它的开头一段 import 就暴露了整个仓库的协作方式：

- [web_demo_mm.py:L1-L24](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L1-L24)：脚本头部导入 `transformers` 的模型与 processor、`gradio` 界面库，并在 `try/except` 中尝试导入 `vllm` 和 `qwen_vl_utils.process_vision_info`——**根目录脚本自己不实现视觉预处理，而是把 qwen-vl-utils 当作 pip 依赖来用**。这个 import 关系是理解四大子项目关系的钥匙（详见 4.2）。

- [requirements_web_demo.txt:L1-L16](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/requirements_web_demo.txt#L1-L16)：web 演示的依赖清单，核心依赖只有 gradio、torch、transformers、accelerate 几项，flash-attn 被注释为可选项——说明"最小可用"的门槛很低。

- [docker/](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker)：仅含 `Dockerfile-qwen3vl-cu128`（CUDA 12.8 的推理镜像）和 `docker_web_demo.sh`（一键起 web 演示）。注意 docker 目录服务于**推理演示**，不服务于微调。

#### 4.1.4 代码实践

**实践目标**：亲手生成仓库的两级目录树，确认"根目录不是 Python 包"这一判断。

**操作步骤**：

1. 在仓库根目录执行（`tree` 不存在时可先 `sudo apt-get install tree` 或改用下一条）：

   ```bash
   tree -L 2 -I '.git' --dirsfirst
   # 或者不装 tree 的替代方案：
   find . -maxdepth 2 -not -path './.git*' -not -name '.' | sort
   ```

2. 检查输出中根目录下的文件清单，确认只有 `LICENSE`、`README.md`、`requirements_web_demo.txt`、`web_demo_mm.py` 四个文件加上 `cookbooks/`、`docker/`、`evaluation/`、`qwen-vl-finetune/`、`qwen-vl-utils/` 五个目录。
3. 执行 `ls *.py *.toml *.cfg 2>/dev/null`，验证根目录没有打包配置文件。

**需要观察的现象**：目录树中唯一的 `pyproject.toml` 出现在 `qwen-vl-utils/` 里面，而不是根目录。

**预期结果**：得到一份与下文 4.2-4.4 一致的目录树；确认根目录只是"门面 + 演示"，真正可安装的 Python 包只有 qwen-vl-utils 一个。（本实践为纯文件操作，可直接执行验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么根目录的 `web_demo_mm.py` 不需要在本仓库内实现图像下载、缩放等逻辑？

**参考答案**：因为它通过 `from qwen_vl_utils import process_vision_info` 直接复用了 qwen-vl-utils 子项目（见 [web_demo_mm.py:L17-L21](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L17-L21) 的 try/except 导入块）。qwen-vl-utils 已发布到 PyPI，也可以本地 `pip install -e ./qwen-vl-utils` 后使用。

**练习 2**：如果只想快速体验模型对话（不写代码），根目录里哪两个文件配合即可完成？

**参考答案**：`requirements_web_demo.txt`（装依赖）+ `web_demo_mm.py`（启动 Gradio 界面）；需要容器化时再配合 `docker/docker_web_demo.sh`。

### 4.2 qwen-vl-utils 子项目

#### 4.2.1 概念说明

`qwen-vl-utils` 是仓库里**唯一一个标准化的、可独立发布的 Python 包**，职责非常单一：把用户消息里的图像/视频（本地路径、URL、base64、PIL 对象、视频文件、帧目录等多种形态）统一转换成模型 processor 需要的 PIL 图像列表和视频张量列表，并在此过程中完成**像素预算控制**（缩放、对齐、抽帧）。

一句话定位：**输入侧的"翻译官+裁缝"**——翻译各种输入形态，裁剪到模型吃得下的尺寸。单元二将逐函数精读它，本讲只需建立结构认知。

#### 4.2.2 核心流程

```text
messages（含 image/video 字段的对话列表）
    ↓ extract_vision_info()          抽出所有视觉元素及其控制参数
    ↓ process_vision_info()          总调度入口
    ├── fetch_image()                单图：下载/解码 + smart_resize 像素预算缩放
    └── fetch_video()                视频：smart_nframes 抽帧 + 解码 + 单帧分辨率限制
    ↓
(images, videos, video_kwargs, video_metadata) → 交给 transformers 的 processor
```

#### 4.2.3 源码精读

- [qwen-vl-utils/pyproject.toml:L1-L16](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/pyproject.toml#L1-L16)：包定义。包名 `qwen-vl-utils`，版本 `0.0.14`，运行时依赖仅 `requests / pillow / av / packaging` 四项（非常轻量，没有 torch！）——因为它是纯预处理库，视频解码走 PyAV。这解释了为什么它可以被 web demo、评测脚本等任何场景低成本复用。

- [qwen-vl-utils/pyproject.toml:L54-L56](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/pyproject.toml#L54-L56)：`packages = ["src/qwen_vl_utils"]` 声明了 src 布局。**这就是 `pip install -e ./qwen-vl-utils` 能生效的原因**——hatchling 构建后端据此知道把哪个目录装成 `qwen_vl_utils` 包。

- [qwen-vl-utils/src/qwen_vl_utils/__init__.py:L1-L7](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/__init__.py#L1-L7)：整个库的公共 API 只有 5 个函数——`extract_vision_info`、`fetch_image`、`fetch_video`、`process_vision_info`、`smart_resize`。阅读策略：**外部使用者只需要认识 `process_vision_info`，其余 4 个是它的内部工件**。

- [qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L93](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L93)、[L403](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L403)、[L501](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L501)：533 行的实现文件中，`fetch_image`（第 93 行起）、`fetch_video`（第 403 行起）、`process_vision_info`（第 501 行起）三个函数是主链路，行号位置可作为单元二精读时的"锚点"。

- [qwen-vl-utils/README.md:L96-L141](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/README.md#L96-L141)：Qwen3VL 小节。与 Qwen2VL/2.5VL 小节对比可发现 Qwen3-VL 时代新增了 `image_patch_size=16`、`return_video_metadata=True` 等参数，并需要从返回的 videos 中解包 `video_metadata`——这正是 u1-l1 讲过的"文本-时间戳对齐"机制在工程侧的接口体现。

#### 4.2.4 代码实践

**实践目标**：把 qwen-vl-utils 以可编辑方式装进环境，验证导入成功并打印版本号。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   pip install -e ./qwen-vl-utils
   ```

2. 新建 `check_utils.py`（**示例代码**，属于你的实践产物而非仓库文件）：

   ```python
   # 示例代码：验证 qwen-vl-utils 安装
   from qwen_vl_utils import process_vision_info, smart_resize
   from qwen_vl_utils.vision_process import fetch_image, fetch_video, extract_vision_info
   from importlib.metadata import version
   import qwen_vl_utils
   print("模块位置:", qwen_vl_utils.__file__)
   print("包版本:", version("qwen-vl-utils"))
   ```

3. 运行 `python check_utils.py`。

**需要观察的现象**：`__file__` 打印出来的路径应指向你仓库内的 `qwen-vl-utils/src/qwen_vl_utils/__init__.py`（可编辑安装的特征），而不是 site-packages 里的副本。

**预期结果**：版本号与 [pyproject.toml:L3](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/pyproject.toml#L3) 中的 `0.0.14` 一致。注意一个细节：`qwen_vl_utils.__init__.py` 里**没有定义 `__version__` 属性**，所以必须用 `importlib.metadata.version()` 查询元数据，直接 `qwen_vl_utils.__version__` 会报 `AttributeError`。（本实践可在本地直接验证。）

#### 4.2.5 小练习与答案

**练习 1**：qwen-vl-utils 的核心依赖里为什么没有 torch，却能被所有训练/推理脚本使用？

**参考答案**：见 [pyproject.toml:L8-L13](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/pyproject.toml#L8-L13)，它只依赖 requests/pillow/av/packing。图像处理用 Pillow，视频解码用 PyAV（av 包），返回的视频最终才会由调用方环境里的 torch 转成张量。轻依赖使它可以嵌入任何环境。

**练习 2**：如果直接从 PyPI `pip install qwen-vl-utils`，与本仓库源码会有什么关系上的差别？

**参考答案**：功能同源，但 PyPI 版本是快照发布（如 0.0.14），而 `pip install -e ./qwen-vl-utils` 直连本仓库最新源码，随仓库更新即时生效。开发调试仓库内的预处理逻辑时必须用后者。

### 4.3 qwen-vl-finetune 子项目

#### 4.3.1 概念说明

`qwen-vl-finetune` 是 **SFT（Supervised Fine-Tuning，有监督微调）框架**，基于 Hugging Face Trainer 扩展而来，支持 Qwen2-VL / Qwen2.5-VL / Qwen3-VL 三代模型（含 MoE 版本）的微调，并提供 LoRA 与 DeepSpeed 分布式训练能力。

一句话定位：**训练侧的完整流水线**——从标注 JSON 到数据集注册、样本打包，再到多卡训练。它与 qwen-vl-utils 是仓库内唯二包含实质 Python 包的子项目，但注意：**它没有 pyproject.toml，不能 pip 安装**，使用方式是在该目录下用 `torchrun` 直接启动脚本。

#### 4.3.2 核心流程

官方 README 把使用方式概括为两步（定制数据集 → 改训练脚本），其内部结构为：

```text
qwen-vl-finetune/
├── README.md          ← 结构与数据格式的第一手文档
├── qwenvl/            ← 核心 Python 包
│   ├── train/         训练主流程
│   │   ├── train_qwen.py   训练入口（torchrun 启动的 main 文件）
│   │   ├── trainer.py      扩展自 HF Trainer 的定制 trainer
│   │   └── argument.py     三组参数 dataclass
│   └── data/          数据侧
│       ├── __init__.py          数据集注册表（data_dict）
│       ├── data_processor.py    LazySupervisedDataset + 两种 DataCollator
│       └── rope2d.py            MRoPE 二维位置编码（全仓库最接近模型内部机制的代码）
├── scripts/           各规格模型 + 各档 DeepSpeed 的启动脚本模板
├── tools/             pack_data.py（数据打包）、check_image.py（数据校验）、process_bbox.ipynb（grounding 标注转换）
└── demo/              可直接训练的示例数据（单图 JSON + 视频 JSON + 素材）
```

#### 4.3.3 源码精读

- [qwen-vl-finetune/README.md:L8-L25](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L8-L25)：官方 Repository Structure 小节，逐文件说明 `train/`（trainer、train_qwen、argument）、`data/`（数据集配置、data_processor、rope2d）、`tools`（bbox 转换、数据打包）的分工。**这是读这个子项目的正确起点**。

- [qwen-vl-finetune/README.md:L26-L38](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L26-L38)：Requirements 小节给出经官方验证的版本组合：`torch==2.6.0`、`transformers==4.57.0.dev0`、`deepspeed==0.17.1`、`flash_attn==2.7.4.post1`、`peft==0.17.1` 等。注意这里 transformers 用的是 **dev 版本**（训练时代模型代码尚未合入正式版），而根 README 的推理要求是 `>=4.57.0` 正式版——两个子项目的依赖要求不同，建议隔离环境。

- [qwen-vl-finetune/README.md:L42-L104](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L42-L104)：数据格式定义——`image/video` 字段放媒体路径，对话放在 `conversations`（`from: human/gpt` 交替），提示文本中用 `<image>`/`<video>` 占位符与媒体一一对应，并给出单图、多图、视频、grounding、packed 五类完整示例。

- [qwen-vl-finetune/README.md:L157-L209](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L157-L209)：数据集注册机制——在 `data/__init__.py` 里定义 `{"annotation_path": ..., "data_path": ...}` 字典并加入 `data_dict`，训练时用 `数据集名%采样率`（如 `my_dataset%50`）引用。这是单元四实践的入口。

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:L1-L206](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L1-L206)：训练入口文件，仅 206 行，麻雀虽小五脏俱全：按模型名自动选择模型类、LoRA 分支、数据模块组装、Trainer 循环。单元五第一讲将逐段精读。

- [qwen-vl-finetune/demo/](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo)：官方提供的最小训练数据（`single_images.json`、`video.json` 及配套图片/视频素材），README 第 155 行明确说明这两个 JSON 可直接用于训练——**这让"跑通一次微调"的门槛降到只需要一个模型权重**。

#### 4.3.4 代码实践

**实践目标**：不改任何代码，只通过"读结构 + 查配置"确认这个子项目的启动方式与数据入口。

**操作步骤**：

1. 打开 [qwen-vl-finetune/scripts/](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts) 目录，浏览 `sft_qwen3_4b.sh`、`sft_30a3b_lora.sh`、`zero2.json` / `zero3.json` / `zero3_offload.json` 的文件名。
2. 任选一个 `sft_*.sh` 打开，找到其中的 `torchrun ... qwenvl/train/train_qwen.py` 一行，确认启动入口。
3. 打开 [qwen-vl-finetune/demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json)，对照 README 的数据格式，数一数每条样本里 `<image>` 占位符与 `image` 字段的数量关系。

**需要观察的现象**：启动脚本通过 `torchrun`（分布式启动器）直接运行 `qwenvl/train/train_qwen.py`，路径是相对于 `qwen-vl-finetune/` 目录的；demo JSON 中每条 human 消息里 `<image>` 出现的次数等于 `image` 字段引用的图片数。

**预期结果**：确认微调子项目"无安装、脚本启动"的使用范式；确认数据格式中占位符与媒体一一对应的硬性约束。（纯阅读实践，可直接完成。）

#### 4.3.5 小练习与答案

**练习 1**：`qwen-vl-finetune/qwenvl/data/rope2d.py` 为什么值得单独留意？

**参考答案**：u1-l1 讲过 Qwen3-VL 的 Interleaved-MRoPE 位置编码，而模型结构代码在 transformers 中、本仓库唯一"最接近模型内部机制"的实现就是这个 rope2d.py（提供 RoPE 位置 id 计算），它支撑训练时的位置编码构造，是单元五 u5-l4 的精读对象。

**练习 2**：我想在已有的 V100 单卡上微调 2B 模型，应该优先看 scripts 里的哪类文件？

**参考答案**：先看最小规格的 `sft_qwen3_4b.sh`（了解参数模板），再根据显存选择 DeepSpeed 配置档（`zero2.json` 显存充裕时 / `zero3.json` 需切分参数时 / `zero3_offload.json` 显存最紧张时）。具体取舍在 u5-l8 详解。

### 4.4 evaluation 与 cookbooks 子项目

#### 4.4.1 概念说明

这两个子项目代表"模型之外"的两类应用工程，可对照着记：

- **evaluation**：**标准化基准评测的脚手架**。收录 MMMU（图文多学科）、VideoMME（视频理解）、MathVision（数学视觉）、RealWorldQA（真实世界问答）、ODinW-13（开放域目标检测）五个基准，每个目录结构完全同构——这是刻意设计，学会一个就等于学会全部五个。
- **cookbooks**：**能力演示笔记本集**。13 个 Jupyter notebook 按能力组织（识别、OCR、文档解析、2D/3D 定位、视频理解、手机/电脑 Agent、多模态编码、长文档、空间理解等），每个 notebook 教一种"怎么用好提示词"的实战模式。

一句话定位：evaluation 是"考试卷"，cookbooks 是"招式谱"。前者输出分数，后者输出提示词工程经验。

#### 4.4.2 核心流程

evaluation 五个目录的**同构三件套**模式：

```text
evaluation/<基准名>/
├── run_<基准名>.py     ← 主入口：加载 HF 数据集 → 组 prompt → vLLM 推理 → 判分
├── dataset_utils.py    ← 数据侧：题目加载、图像导出、prompt 构建
├── eval_utils.py       ← 判分侧：答案抽取、匹配规则、（必要时）GPT 兜底判分
├── common_utils.py     ← 共用工具（部分基准有）
├── infer_instruct.sh / infer_think.sh    ← Instruct 版推理脚本
├── eval_instruct.sh / eval_think.sh      ← 对应判分脚本
└── requirements.txt / README.md
```

cookbooks 的组织方式则简单得多：`cookbooks/*.ipynb` 按能力命名，`cookbooks/utils/` 放共用的工具模块（如 Agent 函数调用的 `agent_function_call.py`），`cookbooks/assets/` 放素材。

#### 4.4.3 源码精读

- [evaluation/mmmu/](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu)：以 MMMU 目录为样本可看到完整的"三件套 + 双模式脚本"结构（`run_mmmu.py`、`dataset_utils.py`、`eval_utils.py`、`common_utils.py`，以及 infer/eval 各两档 shell 脚本）。

- [evaluation/VideoMME/](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME)、[evaluation/MathVision/](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/MathVision)、[evaluation/RealWorldQA/](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/RealWorldQA)、[evaluation/ODinW-13/](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13)：逐一对照这四个目录的文件清单，可验证"结构同构"的说法成立——五个目录都由同样的组件构成，只是 `run_*.py` 的后缀随基准名变化。单元六将利用这个规律精讲一个、横向对比四个。

- [README.md:L113-L126](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L113-L126)：cookbooks 的权威索引表（12 项能力，每行一个 notebook 及其 Colab 入口）。注意仓库里实际有 13 个 notebook（如 `german_document_ocr.ipynb` 未列入表格），以目录为准。

- [cookbooks/utils/agent_function_call.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/utils/agent_function_call.py)：cookbooks 中唯一的实质性 Python 模块，实现基于 qwen_agent 的 `mobile_use` 工具（GUI Agent 的动作集定义）。它是"笔记本为主、工具为辅"的 cookbooks 结构里最值得精读的代码，单元七 u7-l2 的主角。

#### 4.4.4 代码实践

**实践目标**：用脚本验证 evaluation 五个目录的结构同构性，并为四个子项目各写一句中文定位。

**操作步骤**：

1. 在仓库根目录执行（**示例代码**，验证同构性）：

   ```bash
   for d in evaluation/*/; do
     echo "== $d"; ls "$d" | sed 's/^/   /'
   done
   ```

2. 观察五个目录的文件列表，找出共同出现的文件名模式（`run_*.py`、`dataset_utils.py`、`eval_utils.py`、`infer_*.sh`、`eval_*.sh`）。
3. 在 `Qwen3-VL-tutorial/` 之外的任意笔记位置（比如你自己的笔记本），写下四句定位，参考措辞：
   - qwen-vl-utils：把各种形态的图像/视频输入统一加工成模型可吃入的图像与视频张量的预处理库；
   - qwen-vl-finetune：基于 HF Trainer 扩展的三代 Qwen-VL 有监督微调框架；
   - evaluation：五个结构同构的标准基准评测脚手架；
   - cookbooks：按能力组织的提示词实战笔记本集。

**需要观察的现象**：五个评测目录中，每个目录都有 `dataset_utils.py`、`eval_utils.py` 和四个 `infer/eval × instruct/think` 组合的 shell 脚本；差异只在 `run_*.py` 的名字和是否有 `common_utils.py`。

**预期结果**：输出五组几乎相同的文件清单，证实"学一个等于学五个"；四句定位能让你在不看文档的情况下向别人讲清各子项目职责。（本实践可直接执行验证。）

#### 4.4.5 小练习与答案

**练习 1**：评测目录里为什么每个基准都提供 `infer_instruct.sh` 和 `infer_think.sh` 两套脚本，而不是一套？

**参考答案**：u1-l1 讲过每个规格都有 Instruct 与 Thinking 两个独立 checkpoint，且**官方采样超参不同**（Thinking 模型需要更长输出与不同采样参数）。推理与判分按模型类型分档，评测报告才与官方对得上。

**练习 2**：我想给项目新增一个自己的评测集（比如自建的图表问答集），最省力的路径是什么？

**参考答案**：复制一个结构最接近的现有目录（图文问答选 `evaluation/mmmu/`，视频选 `evaluation/VideoMME/`），保留三件套骨架，替换数据加载与判分逻辑。同构设计让迁移成本最低，这正是单元六 u6-l1 要抽提的通用模式。

**练习 3**：四大子项目中，哪两个是"可直接运行产出结果"的，哪两个是"被别人引用的库/资产"？

**参考答案**：qwen-vl-finetune（torchrun 启动训练）和 evaluation（run_*.py 产出分数）是直接运行的；qwen-vl-utils 是被根目录脚本和评测脚本 import 的库；cookbooks 是供人学习的笔记本资产（笔记本也可运行，但定位是示例而非产品）。

## 5. 综合实践

**任务：制作一份《Qwen3-VL 仓库导览图》笔记，并在真实环境完成一次"子项目联通验证"。**

把本讲四个模块串起来：

1. **画树**（对应 4.1）：生成仓库两级目录树（`tree -L 2 -I '.git'`），贴进笔记。
2. **标注**（对应 4.2-4.4）：在树上为四个子项目各加一句中文定位说明，并各标出它的"第一入口文件"：
   - qwen-vl-utils → `src/qwen_vl_utils/vision_process.py`（逻辑）与 `pyproject.toml`（安装）
   - qwen-vl-finetune → `qwenvl/train/train_qwen.py`（训练入口）与 `README.md`（结构文档）
   - evaluation → 任一 `run_*.py`（如 `evaluation/mmmu/run_mmmu.py`）
   - cookbooks → 根 README 的 Cookbooks 表格（L113-L126）
3. **联通验证**（对应 4.2.4 的实践延伸）：`pip install -e ./qwen-vl-utils` 后，写一段最小代码调用 `process_vision_info` 处理一条含本地图路径的消息，确认这个"被引用的库"真正能在你环境里工作：

   ```python
   # 示例代码：最小联通验证
   from qwen_vl_utils import process_vision_info
   messages = [{"role": "user", "content": [
       {"type": "image", "image": "file:///任意/一张/你电脑上的图.jpg"},
       {"type": "text", "text": "描述这张图"},
   ]}]
   images, videos = process_vision_info(messages)
   print(type(images[0]), images[0].size)
   ```

   预期输出 `<class 'PIL.Image.Image'>` 和一个 `(宽, 高)` 元组，且宽高是被 smart_resize 规整过的尺寸（细节在 u2-l2 展开）。如本地无 GPU/环境未就绪，步骤 3 标注「待本地验证」，前两步不受影响。

4. **自测**：合上笔记，回答三个问题——"想改视频抽帧策略找哪个子项目？""想加训练数据找哪个子项目？哪个文件？""想复现官方跑分找哪个子项目？"。三题全对即本讲达标。

## 6. 本讲小结

- 本仓库是**多合一工程集合**：根目录只有门面文件（README、LICENSE）与开箱演示（`web_demo_mm.py`、`requirements_web_demo.txt`、`docker/`），不含任何模型结构代码。
- **qwen-vl-utils** 是唯一可 pip 安装的子项目（src 布局 + hatchling），公共 API 仅 5 个函数，核心入口 `process_vision_info`；它是 web demo 与评测脚本共同的视觉预处理依赖。
- **qwen-vl-finetune** 是基于 HF Trainer 的微调框架，`qwenvl` 包分 `train/`（入口 train_qwen.py）与 `data/`（注册表、data_processor、rope2d）两翼，配套 scripts/ 启动模板与 tools/ 数据工具，采用 torchrun 直启、不做 pip 安装。
- **evaluation** 的五个基准目录结构完全同构（run_*.py + dataset_utils.py + eval_utils.py + Instruct/Think 双档脚本），学一个即可通吃五个。
- **cookbooks** 以 13 个能力笔记本为主体，配套 `utils/agent_function_call.py` 等少量共用工具，是提示词与 Agent 工具调用的实战教材。
- 判断"改哪里"的口诀：**输入找 utils、训练找 finetune、跑分找 evaluation、用法找 cookbooks**。

## 7. 下一步学习建议

下一讲 **u1-l3《第一次推理：单图、多图与视频》** 将带你用 transformers 的 `AutoModelForImageTextToText` + `AutoProcessor` 实际跑通三种输入的推理，届时本讲的 qwen-vl-utils 将第一次在真实调用链中登场（`apply_chat_template` → `process_vision_info` → `model.generate`）。

如果想提前建立感性认识，推荐现在就通读两个文件：

1. [README.md:L144-L193](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L144-L193)——根 README 的 transformers 推理完整示例（u1-l3 的实践蓝本）。
2. [qwen-vl-utils/README.md:L96-L141](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/README.md#L96-L141)——Qwen3VL 小节的用法对照，注意与 Qwen2VL/2.5VL 小节比较新增的 `image_patch_size` 与 `return_video_metadata` 参数，它们是单元二的伏笔。
