# 开发环境搭建与多种运行方式

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解「为什么 AI 课程需要虚拟环境」，并用 `conda` 创建本课程专用的 `ai4beg` 环境。
- 读懂 `environment.yml` 与 `requirements.txt` 两个文件各自负责装什么，并能本地启动 Jupyter、为 Notebook 选择正确的内核（kernel）。
- 在「本地、GitHub Codespaces、Binder、Google Colab、容器」五种运行方式之间做出合理取舍，知道每种方式适合什么场景。

一句话定位：**上一讲（u1-l2）我们知道了 `lessons/` 下躺着大量可执行的 `.ipynb` 笔记本，本讲就是解决「怎么把这些笔记本真正跑起来」。**

## 2. 前置知识

在动手之前，先建立三个基本概念。如果你已经熟悉，可以跳到第 3 节。

### 2.1 虚拟环境（Virtual Environment）

不同项目依赖的库版本常常冲突（比如 A 项目要 TensorFlow 2.13，B 项目要 2.17）。**虚拟环境**就是给每个项目建一个独立的 Python「小房间」，房间里的库互不干扰。本课程用一个名为 `ai4beg` 的房间。

### 2.2 conda 与 miniconda

`conda` 是一个跨语言的包管理器，既能装 Python 包，也能装 C 库（比如 OpenCV 依赖的底层库）。`miniconda` 是 conda 的精简发行版，只包含 conda 本体 + 一个 Python，体积小、够用。本课程的安装方式就以 miniconda 为推荐。

### 2.3 Jupyter Notebook 与内核（Kernel）

`.ipynb` 文件叫 **Jupyter Notebook**，是一种「代码 + 文字 + 图表」混排的文档，非常适合教学。它的运行依赖一个叫 **kernel（内核）** 的后台进程——kernel 才是真正执行 Python 代码、保存变量状态的那个东西。我们稍后会看到，启动 Jupyter 后必须**为每个 Notebook 选对 kernel（指向 `ai4beg` 环境）**，否则 `import torch` 会报「找不到模块」。

> 名字冷知识：Jupyter = **Ju**lia + **Pyt**hon + **R**，最初支持这三种语言。本课程只用其中的 Python 内核。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [environment.yml](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/environment.yml) | 用 conda 语法声明 `ai4beg` 环境要装哪些包（含 PyTorch 全家桶）。 |
| [requirements.txt](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/requirements.txt) | 用 pip 语法声明 conda 装不了的包（主要是 TensorFlow/Keras 全家桶）。被 `environment.yml` 引用。 |
| [lessons/0-course-setup/setup.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/setup.md) | 「课程入门」导航页，介绍如何使用本课程，并指向真正的安装说明 `how-to-run.md`。 |
| [lessons/0-course-setup/how-to-run.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md) | **真正给出 conda/Jupyter/云端命令的技术文档**，本讲多数命令都来自这里。 |
| [.devcontainer/devcontainer.json](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.devcontainer/devcontainer.json) | 告诉 GitHub Codespaces / VS Code 远程容器如何自动构建环境。 |

> 重要更正：本讲的规格里把 conda 命令归在 `setup.md` 名下，但**真正的命令其实写在 `how-to-run.md` 里**；`setup.md`（第 1 行标题就是 "Getting Started with this Curricula"）更像是一篇「如何使用本课程」的总览，它通过一条链接（[setup.md:10](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/setup.md#L10)）把你引到 `how-to-run.md`。后面我们把两者配合起来读。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 conda 环境创建**——读懂并执行 `environment.yml` + `requirements.txt`。
2. **4.2 Jupyter 启动与内核**——本地起 Jupyter，给笔记本挂上 `ai4beg` 内核。
3. **4.3 云端运行方式**——Codespaces / Binder / Colab / 容器，以及横向对比。

---

### 4.1 conda 环境创建

#### 4.1.1 概念说明

本课程的依赖分两层：

- **conda 层**（`environment.yml`）：装那些和底层 C 库耦合较紧、或体积大的包，比如 Python 解释器本身、Jupyter、NumPy、SciPy、OpenCV，以及 PyTorch 全家桶。
- **pip 层**（`requirements.txt`）：装 conda 仓库里不那么齐全的包，主要是 TensorFlow / Keras 相关。

两层不是并列选择，而是**嵌套**：`environment.yml` 的末尾用一句 `-r requirements.txt` 把 pip 清单「挂载」进来，所以你只需要对 conda 下一条命令，两层就都装好了。

#### 4.1.2 核心流程

本地搭建环境的完整流程可以用伪代码描述：

```text
1. 安装 miniconda（一次性，得到 conda 命令）
2. git clone 仓库并 cd 进去
3. conda env create --name ai4beg --file <某个 environment.yml>
       └─ conda 读 environment.yml
            ├─ 装 conda 层依赖（python, jupyter, pytorch...）
            └─ 触发 pip: 装 requirements.txt（tensorflow, keras...）
4. conda activate ai4beg          # 进入这个"小房间"
5. jupyter notebook               # 启动（见 4.2）
```

#### 4.1.3 源码精读

先看根目录的 `environment.yml`，它只有 24 行，却定义了整个环境：

[environment.yml:1-24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/environment.yml#L1-L24) —— 用 conda 语法声明名为 `ai4beg` 的环境，渠道取 `defaults` 和 `conda-forge`，依赖项包括 Jupyter、NumPy、SciPy、OpenCV 等。

其中最值得注意的三块：

- 第 1 行 `name: ai4beg` 给环境起名，后面 `conda activate ai4beg` 就靠它。
- [environment.yml:19-22](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/environment.yml#L19-L22) 是 **PyTorch 全家桶**（`pytorch` / `torchtext` / `torchvision` / `torchdata`），且都从 `pytorch::` 这个专用渠道装——这是计算机视觉、NLP 后面大量 Notebook 的基础。
- [environment.yml:23-24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/environment.yml#L23-L24) 的 `pip:` 段，用 `-r requirements.txt` 把 pip 清单挂进来。

再看被挂载的 `requirements.txt`，它负责 TensorFlow/Keras 这一侧：

[requirements.txt:1-20](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/requirements.txt#L1-L20) —— 用 `==` 精确锁版本（如 `tensorflow==2.17.0`、`keras==3.13.2`），保证全球学习者装出来的环境一致、能复现课程结果。

> 这正好印证了 u1-l1 讲过的「**双框架并行**」：这里同时装了 PyTorch（conda 层）和 TensorFlow/Keras（pip 层），你看哪个框架的笔记本都跑得起来，学一个就行。

**一个容易踩坑的真实细节**：`how-to-run.md` 里让你执行的命令是

```bash
conda env create --name ai4beg --file .devcontainer/environment.yml
```

（见 [how-to-run.md:14](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md#L14)），指向的是 `.devcontainer/environment.yml` 而非根目录的 `environment.yml`。但你不必纠结：**这两个文件内容逐字节相同**（根目录那份是它的副本）。所以无论写 `--file environment.yml` 还是 `--file .devcontainer/environment.yml`，结果完全一样。

#### 4.1.4 代码实践

**实践目标**：亲手创建 `ai4beg` 环境，并验证它装了 PyTorch 和 TensorFlow 两个框架。

**操作步骤**：

1. 从 [miniconda 官网](https://conda.io/en/latest/miniconda.html) 安装 miniconda（这一步在仓库外完成，待本地验证）。
2. 在仓库根目录执行（命令取自 `how-to-run.md`）：

   ```bash
   git clone http://github.com/microsoft/ai-for-beginners
   cd ai-for-beginners
   conda env create --name ai4beg --file .devcontainer/environment.yml
   conda activate ai4beg
   ```

3. 激活后，在终端里分别执行（这两行是**示例代码**，仅用于自检，非仓库原有脚本）：

   ```bash
   python -c "import torch; print('torch', torch.__version__)"
   python -c "import tensorflow as tf; print('tf', tf.__version__)"
   ```

**需要观察的现象**：第 2 步首次执行会下载大量包，耗时较长（可能十几分钟）；第 3 步应分别打印出版本号。

**预期结果**：torch 与 tensorflow 都能 `import` 成功并打印版本（torch 由 conda 装，tf 由 pip 装），说明两层依赖都正确挂载。

**无法确定的部分**：具体版本号、下载耗时取决于你的网络与机器，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么要把依赖拆成 `environment.yml`（conda）和 `requirements.txt`（pip）两层，而不是全写进一个文件？
**答案**：因为有些包（如 PyTorch、OpenCV）在 conda 渠道里带预编译的二进制和底层 C 库，用 conda 装最省心；而 TensorFlow 生态在 conda 渠道并不齐全，用 pip 装更合适。两层分工、再由 `environment.yml` 用 `-r requirements.txt` 串联，是兼顾两者优势的常见做法。

**练习 2**：如果执行 `conda env create` 时卡在 "Solving environment" 很久，可能的原因有哪些？
**答案**：常见原因有渠道优先级冲突（`defaults` 与 `conda-forge` 混用）、网络慢、conda 求解器版本旧。可尝试更新 conda（`conda update conda`）、使用 `libmamba` 求解器，或改用云端方式（见 4.3）绕过本地求解。

**练习 3**：`requirements.txt` 里为什么用 `==` 而不是 `>=`？
**答案**：`==` 锁死精确版本，保证不同人、不同时间装出的环境一致，便于复现课程结果；`>=` 会随时间漂移，今天能跑的笔记本几个月后可能因依赖升级而报错。

---

### 4.2 Jupyter 启动与内核

#### 4.2.1 概念说明

环境装好后，它本身不会自动变成「Jupyter 能用的内核」。我们需要：

1. **启动 Jupyter 服务**：在终端跑 `jupyter notebook`，它会启动一个本地 Web 服务并自动打开浏览器。
2. **让 Jupyter 认识 `ai4beg` 内核**：因为 `environment.yml` 里装了 `ipykernel`（见 [environment.yml:6](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/environment.yml#L6)），在激活的环境里直接运行 `jupyter notebook` 时，该内核通常会被自动识别；若没识别到，需要手动注册一次。

#### 4.2.2 核心流程

```text
conda activate ai4beg                 # 先进"小房间"
jupyter notebook                      # 启动服务，浏览器弹出文件树
  └─ 点击任意 .ipynb 打开
       └─ 顶部菜单 Kernel → Change Kernel → 选 "ai4beg"（或 Python [conda env:ai4beg]）
            └─ 运行第一个 cell，确认 import torch 不报错
```

#### 4.2.3 源码精读

启动命令在 `how-to-run.md` 的「Using Jupyter in the Browser」一节：

[how-to-run.md:30-39](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md#L30-L39) —— 说明进入课程目录后执行 `jupyter notebook` 或 `jupyterhub` 即可在浏览器里打开任意 `.ipynb` 文件开始工作。

其中核心命令就一行：

[how-to-run.md:33](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md#L33) —— `jupyter notebook`，本地启动经典 Jupyter 网页界面。

`environment.yml` 里专门为内核预留了依赖：

[environment.yml:6-9](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/environment.yml#L6-L9) —— `ipykernel`（让 conda 环境能作为 Jupyter 内核）、`ipywidgets`（Notebook 里的交互控件）、`jupyter` 本体，三者共同支撑「在网页里跑笔记本」这件事。

#### 4.2.4 代码实践

**实践目标**：启动 Jupyter，打开任一 Notebook，确认它跑在 `ai4beg` 内核上。

**操作步骤**：

1. 终端里 `conda activate ai4beg` 后，进入仓库任意一课目录，例如：

   ```bash
   cd lessons/3-NeuralNetworks/05-Frameworks
   jupyter notebook
   ```

2. 浏览器自动打开文件树，点击 `IntroPyTorch.ipynb`。
3. 顶部菜单 `Kernel → Change Kernel`，确认选中的是 `ai4beg`（或 `Python [conda env:ai4beg]`）。
4. 如果内核列表里**没有** `ai4beg`，在终端执行（**示例代码**，手动注册内核）：

   ```bash
   python -m ipykernel install --user --name ai4beg --display-name "Python (ai4beg)"
   ```

   然后刷新页面再选。
5. 运行第一个 cell。

**需要观察的现象**：浏览器弹出文件树；切换内核后，第一个 cell（通常是 `import torch` 之类）能无报错地执行完毕，左侧出现执行序号 `[1]`。

**预期结果**：Notebook 在 `ai4beg` 内核下正常运行，`import torch` / `import tensorflow` 不报「No module named」。

**无法确定的部分**：不同 conda/Jupyter 版本下内核是否自动注册存在差异，若需手动注册命令见上，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：明明已经 `conda activate ai4beg`，为什么在 Jupyter 里运行 `import torch` 还报「No module named torch」？
**答案**：因为 Jupyter 执行代码的是**内核**而不是你的终端 shell。你激活的是终端环境，但 Notebook 选的内核可能是系统默认 Python。解决办法：在 Notebook 里 `Kernel → Change Kernel` 切到 `ai4beg`，或用 4.2.4 的命令手动注册内核。

**练习 2**：`jupyter notebook` 和 `jupyterhub` 有什么区别？
**答案**：`jupyter notebook` 是单用户的经典笔记本服务，启动后给自己用；`jupyterhub` 是多用户的服务器版，适合给一个班级/团队每人分配独立环境。本课程个人学习用 `jupyter notebook` 即可。

**练习 3**：`ipykernel` 这个包的作用是什么？删掉它会发生什么？
**答案**：`ipykernel` 提供 Jupyter 与 Python 解释器之间的通信协议（IPython kernel）。删掉后，`ai4beg` 环境就无法作为 Jupyter 内核被识别，Notebook 也就无法在这个环境里执行代码。

---

### 4.3 云端运行方式

#### 4.3.1 概念说明

不是每个人都有条件本地装一套庞大的深度学习环境。本课程贴心地提供了多种「零安装」或「云端 GPU」的方式：

- **GitHub Codespaces**：在 GitHub 上为你开一台云端虚拟机，配 VS Code 网页界面，仓库里的 `.devcontainer` 配置会自动把环境构建好。
- **Binder**：免费、点一下就能在浏览器里跑 Notebook，但算力很弱、且会屏蔽部分外网（影响下载模型/数据集）。
- **Google Colab**：自带免费 GPU，适合后面需要训练的课程；但 Notebook 要一个个手动上传。
- **容器（`.devcontainer` / Docker）**：用 Docker 复现完全一致的环境，适合有经验的学习者。
- **GPU 云主机 / Azure ML**：机构或 Azure for Students 用户可选，算力最强。

#### 4.3.2 核心流程

这几种方式在 `how-to-run.md` 里被分成三档介绍：

[how-to-run.md:45-52](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md#L45-L52) —— 「Running in the Cloud」一节：列出了 GitHub Codespaces 和 Binder 两种无需本地安装 Python 的云端方案。

其中 Binder 的免费代价要特别注意：

[how-to-run.md:52](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md#L52) —— 提醒 Binder 为防滥用会屏蔽部分网络资源，导致从公网下载模型/数据集的代码可能跑不通，且算力很基础，后期复杂课程训练会很慢。

对需要 GPU 的后期课程，文档单独给了一节：

[how-to-run.md:54-64](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md#L54-L64) —— 「Running in the Cloud with GPU」：介绍数据科学虚拟机、Azure ML Workspace，以及末尾提到的 Google Colab（自带免费 GPU，可逐个上传 Notebook 执行）。

#### 4.3.3 源码精读

**Codespaces / VS Code 远程容器**是怎么「自动建环境」的？答案在 `.devcontainer/devcontainer.json`：

[.devcontainer/devcontainer.json:1-31](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.devcontainer/devcontainer.json#L1-L31) —— 声明 Codespaces 用 `mcr.microsoft.com/devcontainers/miniconda` 这个内置 conda 的镜像，并要求至少 2 核 CPU。

关键是它的 `postCreateCommand`：

[.devcontainer/devcontainer.json:30](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.devcontainer/devcontainer.json#L30) —— 容器建好后自动执行 `conda update conda -y && conda env create -f environment.yml`，也就是**你一开 Codespaces，环境就在云端自动建好了**，这正是「零安装」体验的来源。

**Binder** 则走完全独立的另一套配置——它有自己的、版本更老的冻结环境：

[binder/environment.yml:1-25](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/binder/environment.yml#L1-L25) —— Binder 专用环境，锁的是较旧版本（如 `python=3.8.12`、`pytorch=1.11.0`），与根目录 `environment.yml` 不一样。

为什么 Binder 要单独维护一套旧版本？因为 Binder 在有限的免费算力上构建镜像，必须用经过验证的、更稳定保守的版本组合，确保镜像能成功构建并启动。`binder/postBuild.sh` 还会在构建后更新 conda：

[binder/postBuild.sh:1-2](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/binder/postBuild.sh#L1-L2) —— 镜像构建后执行 `conda update -n base -c conda-forge conda`，先把 conda 自身升到最新再交付。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：对比「本地环境」与「Binder 环境」的差异，理解为什么同一门课要维护两套依赖清单。

**操作步骤**：

1. 用 `Read` 打开根目录 [environment.yml](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/environment.yml) 和 [binder/environment.yml](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/binder/environment.yml)。
2. 建一张小表，对比两者的 `python`、`pytorch`、`numpy` 版本。
3. （可选，待本地验证）在浏览器打开 `https://mybinder.org/v2/gh/microsoft/ai-for-beginners/HEAD`，观察 Binder 构建并启动 Jupyter 的过程。

**需要观察的现象**：根目录版本更新（如无 `python=` 锁定、`matplotlib=3.9`），Binder 版本明显更老（`python=3.8.12`、`pytorch=1.11.0`）。

**预期结果**：你能用自己的话说出「Binder 用旧版本是为了在免费弱算力上保证镜像可构建；本地用新版本是为了跟上最新框架」这一取舍。

**无法确定的部分**：Binder 实际启动耗时与可用性受其服务负载影响，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Binder 和 Google Colab 都能云端跑 Notebook，主要区别是什么？
**答案**：Binder 能一键打开整个仓库的 Jupyter、无需上传，但算力弱、会屏蔽部分外网、无 GPU；Colab 有免费 GPU、适合训练，但 Notebook 要一个个手动上传，且不是整个仓库的环境。快速试跑选 Binder，需要训练选 Colab。

**练习 2**：为什么 GitHub Codespaces 能做到「点开即用、自动建环境」？
**答案**：因为仓库根目录有 `.devcontainer/devcontainer.json`，它的 `postCreateCommand` 会在 Codespace 创建后自动执行 `conda env create -f environment.yml`，环境在云端就被构建好了。

**练习 3**：`binder/environment.yml` 的版本为什么比根目录旧？
**答案**：Binder 提供的是免费、有限的算力，构建镜像必须用经过长期验证、稳定保守的旧版本组合，保证镜像能成功构建并启动；而根目录面向本地学习者，倾向于用较新的框架版本。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个端到端任务：**用本地 conda 跑通课程的第一个 Notebook，并建立自己的排错档案。**

**实践目标**：从零创建 `ai4beg` 环境 → 启动 Jupyter → 打开一个真实课程 Notebook 跑通第一个 cell → 把过程中遇到的问题记进 `troubleshoot.md`。

**操作步骤**：

1. **建环境**（命令出自 [how-to-run.md:11-16](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md#L11-L16)）：

   ```bash
   git clone http://github.com/microsoft/ai-for-beginners
   cd ai-for-beginners
   conda env create --name ai4beg --file .devcontainer/environment.yml
   conda activate ai4beg
   ```

2. **起 Jupyter**（命令出自 [how-to-run.md:33](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md#L33)）：

   ```bash
   cd lessons/3-NeuralNetworks/05-Frameworks
   jupyter notebook
   ```

3. **跑第一个 cell**：打开 `IntroPyTorch.ipynb`，确认内核是 `ai4beg`（必要时用 4.2.4 的命令注册），运行第一个 cell，截图保存。
4. **写排错档案**：在 `AI-For-Beginners-tutorial/troubleshoot.md`（自建文件，不在本讲义输出范围内）里按下面的模板记录遇到的任何问题：

   ```markdown
   | 现象 | 可能原因 | 解决办法 | 对应本讲章节 |
   | ---- | ------- | -------- | ----------- |
   | import torch 报 No module | 内核选错 | Change Kernel → ai4beg | 4.2 |
   | ...  | ...     | ...      | ...         |
   ```

**需要观察的现象**：环境创建日志、Jupyter 启动后的浏览器文件树、第一个 cell 的执行输出。

**预期结果**：你拥有一份能跑通 `import torch` 的 `ai4beg` 环境，并有一份贴合自己机器的 `troubleshoot.md`，后续 24 课遇到环境问题都能回查。

**无法确定的部分**：不同操作系统（Windows/macOS/Linux）下 conda 的具体安装步骤、可能遇到的报错各不相同，**具体现象待本地验证并如实记入 troubleshoot.md**。本讲义不假装命令已在你机器上跑过。

> 若本地搭建反复失败，可先用 **Binder / Codespaces**（见 4.3）把课程跑起来，不影响学习进度，等有余力再回头配置本地环境。

## 6. 本讲小结

- 本课程用 `conda` 建一个名为 **`ai4beg`** 的虚拟环境，依赖分两层：`environment.yml`（conda 层，含 PyTorch 全家桶）通过 `-r requirements.txt` 挂载 `requirements.txt`（pip 层，含 TensorFlow/Keras）。
- 启动 Jupyter 用 `jupyter notebook`；关键是为 Notebook 选对 **`ai4beg` 内核**，否则会报「找不到模块」。`environment.yml` 里的 `ipykernel` 就是为此准备的。
- 真正的技术命令在 [how-to-run.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/how-to-run.md)，而 [setup.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/0-course-setup/setup.md) 是指向它的入门导航页。
- 五种运行方式取舍：**本地**适合长期学习、**Codespaces** 零安装且靠 `.devcontainer` 自动建环境、**Binder** 一键试跑但弱、**Colab** 有免费 GPU、**容器**适合复现/有经验者。
- Binder 维护着一套**更旧**的冻结环境（`binder/environment.yml`），这是免费弱算力下保证镜像可构建的取舍。

## 7. 下一步学习建议

环境跑通后，建议这样继续：

1. **立刻动手**：进入下一讲 **u1-l4 从 examples 开始：第一个 AI 程序**，运行 `examples/01-hello-ai-world.py`，用最简单的 Python 脚本（不依赖任何深度学习框架）直观感受「从数据中学习权重」这一 AI 核心思想。
2. **进阶阅读源码**：想深入理解环境如何被自动构建，可精读 [.devcontainer/Dockerfile](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/.devcontainer/Dockerfile)（手动 `conda install` + `pip install` 的容器构建逻辑）。
3. **排错留档**：把综合实践里的 `troubleshoot.md` 持续维护下去——后面 24 课的 Notebook 几乎都要在 `ai4beg` 环境里跑，这份档案会让你越学越顺。
