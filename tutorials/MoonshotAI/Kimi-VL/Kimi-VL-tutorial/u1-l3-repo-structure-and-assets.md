# 仓库结构与模型资产：从 GitHub 仓库到 HuggingFace 权重

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出本仓库的完整文件地图：`README.md`、`requirements.txt`、`LICENSE`、`Kimi-VL.pdf` 技术报告、`figures/` 下的 7 张图片，并说出每个文件「是什么、被谁引用、服务哪一讲」。
2. 解释 `trust_remote_code=True` 时模型实现代码的真实来源：它不在本仓库，而是托管在 HuggingFace 的 `moonshotai/Kimi-VL-A3B-*` 模型仓库里，并能在 README 中指出所有指向 HuggingFace 的入口链接。
3. 弄清 `figures/` 下两类资产的区别：**给读者看的图**（架构图、性能图）与**给模型看的图**（推理示例的输入图片），并了解 `Kimi-VL.pdf` 技术报告的大致结构，为第 4 单元的精读做准备。

本讲是入门单元的收尾。前两讲（u1-l1、u1-l2）已经建立了「项目是什么」和「环境怎么搭」的认知；本讲把目光拉远，看清这个仓库里**每一样东西**的用途——因为仓库很小，小到可以彻底看完，这是大仓库做不到的奢侈。

## 2. 前置知识

本讲不需要写代码，只需要两个前置概念（u1-l1 已建立，这里换角度复述）：

- **发布型仓库（release repo）**：一类 GitHub 仓库不为托管源码，而是作为模型的「发布物集合」——文档、许可证、技术报告、示例资产。Kimi-VL 仓库属于此类：全仓库没有一行模型实现代码。这与「源码型仓库」（如 vLLM、transformers 本身）形成对照。
- **`trust_remote_code=True`**：transformers 的一个加载开关。设为 True 时，`from_pretrained` 除了下载权重，还会从 HuggingFace 模型仓库下载 `.py` 实现文件并在本地执行，从而支持自定义模型结构。Kimi-VL 的模型代码（MoonViT、MoE 解码器、Processor 等）正是通过这条通道进入你的程序的。

再补充两个阅读本讲会用到的小概念：

- **永久链接（permalink）**：形如 `github.com/<owner>/<repo>/blob/<commit>/<path>#L行号` 的 URL。只要 commit 不变，行号永远有效。本手册所有源码引用都用当前 HEAD `41d5ef0` 的永久链接。
- **资产（assets）**：仓库中非代码的辅助文件——图片、PDF、配置清单。它们的「源码」意义在于**被谁引用、引用它做什么**。

## 3. 本讲源码地图

本讲的「源码」是仓库自身的结构。逐项如下：

| 文件 | 大小 | 作用 | 本讲关注的位置 |
| --- | --- | --- | --- |
| `README.md` | 16.5 KB | 仓库唯一的长文档：定位介绍、架构说明、模型变体表、推理/微调/部署全部示例代码 | 9 个章节的整体结构（L13–L311），以及散落各处的 HuggingFace 链接 |
| `requirements.txt` | 97 B | 8 个依赖的安装清单（u1-l2 已逐行精读） | 仅作为文件地图的一员 |
| `LICENSE` | 1.1 KB | MIT 许可证，版权归 Moonshot AI | L1–L2 的版权声明 |
| `Kimi-VL.pdf` | 约 10.5 MB | Kimi-VL 技术报告（Kimi Team, 2025） | 标题页与章节结构（本环境无法解析正文，目录细节待本地验证） |
| `figures/arch.png` | 641 KB | 模型架构图，README 第 2 节引用 | [README.md:L37-L43](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L37-L43) |
| `figures/demo.png` | 525 KB | 单图推理示例的输入图（含穹顶建筑），被 3 段示例代码引用 | L139、L233、L279 |
| `figures/demo1.png`、`figures/demo2.png` | 223 KB / 264 KB | 多图推理示例的输入图（手稿推断任务） | [README.md:L181-L188](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L181-L188) |
| `figures/instruct_perf.png`、`figures/thinking_perf.png` | 2.2 MB / 227 KB | 两个变体的性能对比图，README 第 5 节引用 | [README.md:L85-L93](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L85-L93) |
| `figures/logo.png` | 13 KB | README 页眉的小图标 | [README.md:L6](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L6) |
| `Kimi-VL-tutorial/` | — | 本学习手册的输出目录，**非上游仓库内容**（git 状态中为未跟踪目录） | 不属于仓库资产，阅读时可忽略 |

一句话概括：**这个仓库 = 1 个 README + 1 份依赖清单 + 1 个许可证 + 1 份 PDF 报告 + 7 张图**。其余一切（模型代码、权重、在线 Demo）都活在别处。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 仓库文件地图**、**4.2 模型代码的真实位置**、**4.3 技术报告与示例图资产**。

### 4.1 仓库文件地图

#### 4.1.1 概念说明

读一个陌生仓库的第一步不是打开代码，而是**画地图**：顶层有哪些文件？哪些是文本、哪些是二进制？哪些是「入口」、哪些是「配件」？

对发布型仓库来说，地图尤其简单，因为文件极少。Kimi-VL 仓库根目录只有 5 项上游内容（README、requirements、LICENSE、PDF、figures/），但每项的「信息密度」都很高：

- `README.md` 承担了通常由 `docs/` + `examples/` + `src/` 共同承担的全部职责；
- `Kimi-VL.pdf` 承担了通常由论文/设计文档承担的职责；
- `figures/` 同时充当「文档插图」和「示例数据集」两种角色（4.3 节展开）。

先建立地图，后续所有讲义引用文件时你都能立刻定位。

#### 4.1.2 核心流程

给仓库画地图的标准动作：

1. **列目录**：`ls -la`（或 `git ls-files` 列出全部被跟踪文件），得到文件清单和大小。
2. **分类**：把每个文件归入四类之一——文档（md/pdf）、配置（txt/toml/yaml）、法律（license）、资产（图片等）。
3. **找引用**：对每个资产文件，反查「谁引用了它」——在 README 里搜索文件名即可。
4. **定位入口**：确认没有源码入口（没有 `main.py`、没有 `src/`、没有 `setup.py`/`pyproject.toml`），从而印证「发布型仓库」的判断。

对 Kimi-VL 执行完这四步，得到的地图就是第 3 节那张表。

#### 4.1.3 源码精读

README 是仓库的骨架，它由 9 个章节组成。先看总目录结构：

[README.md:L13-L45](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L13-L45)

这段覆盖第 1–2 节的标题行：`## 1. Introduction`（L13）、`## 2. Architecture`（L37）。后续章节标题依次为 `## 3. News`（L45）、`## 4. Model Variants`（L51）、`## 5. Performance`（L76）、`## 6. Example usage`（L96）、`## 7. Finetuning`（L203）、`## 8. Deployment`（L209）、`## 9. Citation`（L299）。

整理成一张「README 章节 → 手册讲义」的映射表，帮你建立手册与仓库的对应感：

| README 章节 | 行号范围 | 内容 | 对应讲义 |
| --- | --- | --- | --- |
| §1 Introduction | L13–L33 | 模型定位、能力声明、2506 版改进 | u1-l1、u4-l2 |
| §2 Architecture | L37–L43 | 三大组件一句话说明 + arch.png | u3-l1 |
| §3 News | L45–L49 | 版本动态（2506 发布、vLLM/LLaMA-Factory 支持） | u3-l2、u3-l4 |
| §4 Model Variants | L51–L74 | 三变体规格表、温度建议、在线 Demo 链接 | u1-l1、u2-l4 |
| §5 Performance | L76–L93 | 两张性能对比图 | u4-l2 |
| §6 Example usage | L96–L201 | 环境安装 + Transformers 推理两例 | u1-l2、u2-l1、u2-l2 |
| §7 Finetuning | L203–L207 | LLaMA-Factory 微调入口 | u3-l4 |
| §8 Deployment | L209–L297 | vLLM 离线推理与 OpenAI 兼容服务 | u3-l2、u3-l3 |
| §9 Citation | L299–L311 | BibTeX 引用格式 | — |

再看 LICENSE，它是仓库的法律身份：

[LICENSE:L1-L2](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/LICENSE#L1-L2)

这两行声明本仓库内容采用 MIT 许可证、版权归 Moonshot AI（2025）。MIT 是最宽松的开源许可证之一，意味着你可以自由使用、修改、再分发仓库内容（保留版权声明即可）。注意：模型**权重**的许可证在 HuggingFace 模型页单独声明，与本仓库的 MIT 不一定相同，商用前应分别确认。

#### 4.1.4 代码实践

1. **实践目标**：亲手执行「列目录 → 分类 → 找引用」三步，产出一份属于自己的仓库地图。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -la` 和 `ls -la figures/`，记录每个文件的字节数；
   - 执行 `grep -n "figures/" README.md`，找出 README 引用的全部图片路径及所在行号；
   - 新建一个笔记文件（建议放在仓库外的个人笔记目录，或本讲实践区），按「文件名 / 大小 / 类型 / 被引用位置 / 用途」五列整理成表。
3. **需要观察的现象**：README 中每个 `figures/xxx.png` 出现的行号；`requirements.txt` 是否被 README 引用（提示：搜索 `requirements`，会在 Setup 小节找到）。
4. **预期结果**：得到与第 3 节表格一致的地图——7 张图各有一个或多个引用点；`demo.png` 会被 3 段代码引用（Transformers、vLLM 离线、OpenAI API），是全仓库出镜率最高的资产。本讲写作时已验证此结果。

#### 4.1.5 小练习与答案

**练习 1**：仓库里最大的文件和最小的文件分别是什么？这说明了什么？

<details>
<summary>参考答案</summary>

最大的是 `Kimi-VL.pdf`（约 10.5 MB），最小的是 `requirements.txt`（97 字节）。说明仓库的「重量」在文档与报告，而非代码——这是发布型仓库的典型体量分布：一份技术报告比全部工程配置大十万倍。
</details>

**练习 2**：如何用一条命令确认这个仓库里没有任何 Python 源码？

<details>
<summary>参考答案</summary>

`git ls-files "*.py"`——如果输出为空，说明被跟踪的文件中没有 `.py` 文件。（`Kimi-VL-tutorial/` 目录是本手册生成的，未被 git 跟踪，不影响判断。）
</details>

**练习 3**：README 的 §7 Finetuning 一节只有短短 5 行，为什么不觉得它单薄？

<details>
<summary>参考答案</summary>

因为微调能力由外部框架 LLaMA-Factory 提供，本仓库只负责「指路」——给出框架链接与配置说明 PR 的链接。发布型仓库的职责是准确路由到生态入口，而不是复制生态内容。
</details>

### 4.2 模型代码的真实位置

#### 4.2.1 概念说明

u1-l1 已经给出结论：模型实现代码托管在 HuggingFace。本节把这个结论落实到**可点击的链接**和**可追踪的加载机制**上。

先澄清三个容易混淆的「家」：

- **GitHub 仓库**（本仓库）：文档与资产，没有任何可执行模型代码；
- **HuggingFace 模型仓库**（`moonshotai/Kimi-VL-A3B-Instruct` 等）：权重文件 + 模型实现 `.py` 文件 + 配置，`trust_remote_code=True` 加载的就是这里的代码；
- **HuggingFace Space**（`moonshotai/Kimi-VL-A3B-Thinking/`）：在线聊天 Demo，无需任何环境即可体验模型。

`trust_remote_code` 的机制：transformers 的 `from_pretrained` 默认只加载内置架构；当模型是自定义结构时，需要在模型仓库中放置 `.py` 实现文件，并在加载时显式同意执行这些远程代码（`trust_remote_code=True`）。对 Kimi-VL 而言，MoonViT 视觉编码器、MoE 解码器、Processor 的实现都来自这条通道。**具体的 `.py` 文件名以 HuggingFace 模型页的文件列表为准（待确认）**——这正是本节实践要去核实的内容。

#### 4.2.2 核心流程

从「想用模型」到「代码就位」的完整链路：

```text
pip install transformers（u1-l2 已完成）
        │
        ▼
AutoModelForCausalLM.from_pretrained("moonshotai/Kimi-VL-A3B-Instruct",
                                     trust_remote_code=True)
        │
        ├── 1. 访问 huggingface.co/moonshotai/Kimi-VL-A3B-Instruct
        ├── 2. 下载 config.json → 读到 auto_map 字段，指向自定义类
        ├── 3. 下载模型仓库中的 .py 实现文件（真正的模型代码在这里！）
        ├── 4. 本地执行这些 .py，注册 KimiVL 模型类
        └── 5. 下载权重分片并实例化模型
        │
        ▼
AutoProcessor.from_pretrained(..., trust_remote_code=True)  ← 同样从模型仓库加载
```

推论：**离线环境跑不了首次加载**（代码和权重都要从 HuggingFace 拉取）；网络波动或 HuggingFace 模型仓库更新，都可能影响加载结果——这也是 u1-l2 强调「版本组合」的原因之一。

#### 4.2.3 源码精读

README 中所有通往 HuggingFace 的入口集中在页眉和模型变体表。先看页眉：

[README.md:L5-L10](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L5-L10)

三行链接依次是：arXiv 技术报告（L6）、HuggingFace **collection 合集页**（L7，一个页面聚合全部 Kimi-VL 模型仓库）、HuggingFace **Space 在线聊天**（L9，点开就能和 Thinking-2506 对话，零环境要求）。

再看三个模型仓库的官方入口——模型变体表的最后一列：

[README.md:L57-L61](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L57-L61)

表中的 Download Link 分别指向 `moonshotai/Kimi-VL-A3B-Thinking-2506`（L59）、`moonshotai/Kimi-VL-A3B-Instruct`（L60）、`moonshotai/Kimi-VL-A3B-Thinking`（L61，标注 deprecated）。这三个仓库就是权重的家。

最后确认 `trust_remote_code=True` 在示例代码中的落点。Transformers 推理示例的模型加载：

[README.md:L120-L137](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L120-L137)

L120 把 `model_path` 指向 HuggingFace 仓库名（不是本地路径）；L125 与 L137 两处 `trust_remote_code=True` 分别作用于模型和 Processor。同样的参数还出现在：Thinking-2506 示例（L164–L179）、vLLM 离线推理（L226–L231）、vLLM 服务启动命令的 `--trust-remote-code`（L260、L263）。全仓库所有加载路径无一例外都要经过这个开关——这就是「代码在 HuggingFace」的代码级证据。

#### 4.2.4 代码实践

1. **实践目标**：不写一行代码，仅用浏览器核实模型实现代码的真实位置。
2. **操作步骤**：
   - 打开 README 页眉的 HuggingFace 合集链接（L7），记下合集里有哪几个模型仓库；
   - 进入 `moonshotai/Kimi-VL-A3B-Instruct` 模型页，点击 **Files and versions** 标签；
   - 在文件列表中找出所有 `.py` 文件（模型实现、Processor 实现等）以及 `config.json`、权重分片文件，把 `.py` 文件名抄录下来；
   - 顺便打开页眉的 Space 链接（L9），上传或输入任意图片体验一次在线推理。
3. **需要观察的现象**：模型仓库里是否同时存在代码文件（`.py`）、配置（`config.json`）、权重（`.safetensors` 分片）；`config.json` 内容里是否有 `auto_map` 字段指向自定义类。
4. **预期结果**：你会得到一份 `.py` 文件名清单——这份清单就是 u2-l3（Processor 与聊天模板）和 u3-l1（架构解析）将要对照阅读的「真实源码」范围。具体文件名以页面实际显示为准（待本地验证），不要从本文或别处照抄。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AutoProcessor.from_pretrained` 也要传 `trust_remote_code=True`？

<details>
<summary>参考答案</summary>

因为 Processor（含聊天模板、图像预处理）同样是 Kimi-VL 自定义的，其实现代码也托管在 HuggingFace 模型仓库中，不在 transformers 内置库里。不开启的话 transformers 找不到对应的 Processor 类。
</details>

**练习 2**：把 `model_path` 从 `"moonshotai/Kimi-VL-A3B-Instruct"` 换成本地路径行不行？有什么前提？

<details>
<summary>参考答案</summary>

行。`from_pretrained` 既接受 HuggingFace 仓库名也接受本地目录路径，前提是本地目录里已经备齐模型仓库中的全部文件（`.py`、`config.json`、权重分片）。常见做法是先用 `huggingface-cli download` 把仓库拉到本地，再离线加载。
</details>

**练习 3**：GitHub 仓库与 HuggingFace 模型仓库的更新节奏可能不同步。这会带来什么风险？

<details>
<summary>参考答案</summary>

README 示例是针对特定版本的模型代码和 transformers 写的；如果 HuggingFace 侧的 `.py` 实现更新（或 transformers 升级导致接口变化），README 里的旧示例可能出现不兼容。缓解方式：锁定 u1-l2 讲过的版本组合（python=3.10 / torch=2.5.1 / transformers=4.51.3），必要时把模型仓库整体下载到本地固定版本。
</details>

### 4.3 技术报告与示例图资产

#### 4.3.1 概念说明

`figures/` 下的 7 张图分为两类，读者经常混淆：

- **给人看的图（文档插图）**：`logo.png`、`arch.png`、`instruct_perf.png`、`thinking_perf.png`。它们被 README 以 `<img>` 标签或链接引用，作用是让文档更直观。**永远不会进入模型**。
- **给模型看的图（推理输入）**：`demo.png`、`demo1.png`、`demo2.png`。它们出现在示例代码里，被 `Image.open()` 读入后送进模型。**它们是你第一次跑通推理的免费测试数据**——不需要自己找图，仓库自带。

区分方法很简单：看它在 README 里的引用形式。出现在 `image_path = "./figures/demo.png"` 这类代码里的，就是推理输入；出现在 `<img src="figures/arch.png">` 里的，就是文档插图。

`Kimi-VL.pdf` 则是仓库中唯一的长篇技术文档：Kimi-VL 技术报告（README 页眉第一行的链接就指向它，arXiv 编号 2504.07491，见 [README.md:L1-L3](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L1-L3) 与 [README.md:L299-L311](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L299-L311) 的引用信息）。README 里每一条性能声明、每一个架构名词，其完整论证都在这份报告里——它是第 4 单元（u4-l1 精读、u4-l2 基准分析）的主战场。

需要说明：本讲义的写作环境无法解析这份 PDF 的正文（文件为压缩二进制流，且缺少 PDF 渲染工具），因此**报告的具体章节标题在本文中标注为待本地验证**，需要你在实践中亲自打开确认。

#### 4.3.2 核心流程

7 张图的「角色—引用—去向」全景表：

| 图片 | 角色 | 在 README 中的引用位置 | 后续讲义中的去向 |
| --- | --- | --- | --- |
| `logo.png` | 文档插图 | L6（页眉 Tech Report 链接的小图标） | — |
| `arch.png` | 文档插图 | L42（§2 Architecture 内嵌架构图） | u3-l1 架构解析的核心图 |
| `instruct_perf.png` | 文档插图 | L86（§5 Instruct 性能对比图） | u4-l2 基准分析 |
| `thinking_perf.png` | 文档插图 | L92（§5 Thinking 性能对比图） | u4-l2 基准分析 |
| `demo.png` | **推理输入** | L139、L233、L279（三段示例代码） | u2-l1、u3-l2、u3-l3 的实践素材 |
| `demo1.png` | **推理输入** | L181（Thinking-2506 多图示例） | u2-l2 多图推理 |
| `demo2.png` | **推理输入** | L181（同上） | u2-l2 多图推理 |

三段使用 `demo.png` 的代码问了同一个问题（"What is the dome building in the picture?"，可据问题描述推断图中含穹顶建筑），多图示例则用两张手稿图问"这份手稿属于谁、记录了什么"。这种「一张图 × 三种调用方式」的设计很适合学习：**固定输入、变换调用通道**（Transformers → vLLM 离线 → OpenAI API），输出应基本一致，差异只来自工程链路。

#### 4.3.3 源码精读

`arch.png` 在 README 中的引用上下文：

[README.md:L37-L43](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L37-L43)

L39 用一句话概括了架构三件套——MoE 语言模型、原生分辨率视觉编码器 MoonViT、MLP 投影层，L42 内嵌 `figures/arch.png`。这张图就是 u3-l1 的核心教材；现在你只需要知道它在哪、长什么样，不必急于理解每个模块。

推理输入图的引用方式（单图示例）：

[README.md:L139-L145](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L139-L145)

L139 定义图片路径，L140 用 `Image.open` 读成 PIL 对象，L142 的 `messages` 中又出现一次路径（作为模板里的图片占位引用）。注意这个细节：**路径出现两次、角色不同**——一次给 `processor` 的 `images=` 参数（真正读像素），一次给聊天模板（渲染成特殊 token 占位符）。这是 u2-l3 的伏笔。

多图示例的引用方式：

[README.md:L181-L188](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L181-L188)

L181 把两个路径放进列表，L182 批量打开成 `images` 列表，L186–L188 用列表推导为每张图生成一个 `{"type": "image", ...}` 内容块再拼接文字问题。多图与单图的差别只是「列表化」——这个模式将在 u2-l2 展开。

最后是技术报告的双重入口：页眉的本地 PDF 链接（L2，相对路径 `Kimi-VL.pdf`，即仓库里这份 10.5 MB 文件）与 arXiv 链接（L6）。两者内容对应，本地版的好处是**永久可读、随仓库版本走**。

#### 4.3.4 代码实践

1. **实践目标**：打开 `Kimi-VL.pdf` 阅读目录页，抄录章节清单并标注学习优先级，为第 4 单元的精读建立索引。
2. **操作步骤**：
   - 用本地 PDF 阅读器打开仓库根目录的 `Kimi-VL.pdf`（约 40 页量级的技术报告，先别慌，只看目录）；
   - 翻到目录页（通常在第 1–2 页）；若没有目录页，则快速滚动浏览各页顶部的一级标题；
   - 把所有一级章节标题抄录成清单，按「架构 / 训练 / 评测 / 其他」粗分类；
   - 给你最想深入的两到三个章节打星（建议至少包含架构相关章节——它是 u3-l1 与 u4-l1 的共同基础）。
3. **需要观察的现象**：报告是否按「引言 → 相关工作 → 架构 → 预训练 → 后训练/对齐 → 评测 → 结论」这类典型技术报告结构组织；架构章节里 MoonViT 与 MoE 解码器是否各自成节。
4. **预期结果**：得到一份带优先级标注的章节清单。**各章的确切标题与页码待本地验证**——本讲义环境无法解析 PDF 正文，无法替你完成这一步，这正是留给你动手的部分。

#### 4.3.5 小练习与答案

**练习 1**：`figures/demo.png` 在仓库中被哪三段代码引用？它们分别属于哪种调用方式？

<details>
<summary>参考答案</summary>

① Transformers 推理示例（L139，`AutoModelForCausalLM` + `model.generate`）；② vLLM 离线推理示例（L233，`LLM` + `SamplingParams`）；③ vLLM OpenAI 兼容服务的 API 调用示例（L279，图片转 base64 后经 `openai` SDK 发送）。三者分别对应 u2-l1、u3-l2、u3-l3 三讲。
</details>

**练习 2**：为什么说 `demo.png` 是初学者最有价值的资产？

<details>
<summary>参考答案</summary>

因为它是三段官方示例共同的固定输入。固定输入意味着你可以在学习不同调用方式（Transformers / vLLM / API）时排除「图片不同导致答案不同」的干扰，只关注工程链路本身的差异；同时它免去了自找测试图片的成本。
</details>

**练习 3**：README 页眉为什么同时放本地 PDF 相对链接和 arXiv 链接两个入口？

<details>
<summary>参考答案</summary>

本地 `Kimi-VL.pdf`（L2）保证仓库自包含、离线可读、随仓库 commit 固定版本；arXiv 链接（L6）则提供稳定的外部引用入口（便于引用、可能包含勘误后的最新版）。双入口兼顾可复现与可引用。
</details>

## 5. 综合实践

把本讲三个模块串成一个任务：**为 Kimi-VL 仓库制作一份《资产索引文档》**，它是你后续所有讲义实践时随手可查的「地图册」。

任务要求：

1. **文件地图部分**（对应 4.1）：用 `ls -la`、`grep -n "figures/" README.md` 等命令采集信息，产出一张包含仓库全部上游文件的表格，五列：文件名、大小、类型（文档/配置/法律/资产）、被引用位置（行号）、一句话用途。
2. **模型资产部分**（对应 4.2）：记录 HuggingFace 侧的三个入口——collection 合集页、你实际查看的模型仓库文件清单（重点是 `.py` 文件名列表，来自你自己的浏览器观察）、Space 在线 Demo 的体验一句话（试一张图、记一个回答）。
3. **报告索引部分**（对应 4.3）：抄录 `Kimi-VL.pdf` 的章节标题清单，标注你最想深入的两到三个章节及理由。

验收标准（自查）：

- 表格覆盖根目录全部 5 项上游内容与 7 张图片，无遗漏；
- `.py` 文件名清单来自你亲眼所见的模型页文件列表，而非本文转述；
- 章节清单里的标题与 PDF 原文逐字一致。

这份文档不用交作业，它的价值在于：从下一讲（u2-l1）开始，你每次实践都会回到这张地图找素材——`demo.png` 在哪、模型从哪来、架构图在哪，一查便知。

## 6. 本讲小结

- 本仓库 = **README + requirements + LICENSE + 技术报告 PDF + 7 张图**，是典型的发布型仓库，不含任何模型实现代码。
- README 的 9 个章节（Introduction 到 Citation）是全仓库的信息骨架，本手册各讲与之一一对应。
- 模型代码与权重的真实位置在 **HuggingFace 模型仓库**（`moonshotai/Kimi-VL-A3B-*`），通过 `trust_remote_code=True` 在 `from_pretrained` 时下载并执行；入口链接集中在 README 页眉与变体表。
- `figures/` 的 7 张图分两类：4 张**文档插图**（logo、arch、两张性能图）与 3 张**推理输入图**（demo、demo1、demo2）；`demo.png` 被 3 段示例代码共用，是后续实践的标准测试素材。
- `Kimi-VL.pdf`（arXiv 2504.07491）是 README 一切性能与架构声明的完整论证，章节结构需本地打开确认，第 4 单元将精读。
- 给仓库「画地图」的四步法（列目录 → 分类 → 找引用 → 定位入口）适用于任何陌生仓库，本仓库是最好的练手场。

## 7. 下一步学习建议

入门单元到此完成。你已经知道项目是什么（u1-l1）、环境已就绪（u1-l2）、仓库地图已画好（本讲），下一讲进入 **u2-l1「第一次推理：用 Transformers 跑通 Kimi-VL-A3B-Instruct 单图问答」**——用本讲索引到的 `figures/demo.png` 作为输入，逐行拆解 README 中的推理代码。

在开始之前，推荐做两件小事：

1. 到 4.2.4 实践中记录的 HuggingFace 模型页**在线浏览**一遍模型仓库里的 `.py` 文件（只看文件名和开头几十行即可）——u2-l3 讲 Processor 时你会再次需要它们；
2. 如果暂时没有 GPU 环境，可以先打开 README 页眉的 Space 链接，上传 `figures/demo.png` 问一句 "What is the dome building in the picture?"，提前感受一下目标效果——等你跑通本地推理后，可以对比两种通道的回答是否一致。
