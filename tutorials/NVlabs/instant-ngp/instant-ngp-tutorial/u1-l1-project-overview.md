# 项目总览与定位：instant-ngp 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在**不写一行代码**的情况下，建立起对 instant-ngp 的全局认识。读完本讲，你应当能够：

- 用一句话说清楚 instant-ngp 解决的核心问题：**用一个秒级训练的小型 MLP，去表达高频率、高细节的图形信号**。
- 区分它实现的**四种神经图形基元**（NeRF / SDF / 神经图像 / 神经体素），知道每种基元的输入、输出与典型用途。
- 理解本项目最重要的算法思想——**多分辨率哈希编码（multiresolution hash encoding）**——为什么它能让一个很小的网络学得又快又好。
- 弄清 instant-ngp、tiny-cuda-nn、原始 NeRF 论文三者之间的关系。

本讲只讲“它是什么”，不讲“怎么编译、怎么改代码”。那些内容从下一讲（u1-l2 目录结构）开始逐步展开。

---

## 2. 前置知识

本讲面向零基础读者。你只需要对以下几个名词有一个模糊的概念即可，不必深究：

- **神经网络 / MLP（多层感知机）**：一种由若干层线性变换和非线性激活组成的函数拟合器。你可以把它想象成一个“万能近似器”：给它输入，它就输出一个预测值，再用误差反向传播去调整参数。instant-ngp 用的就是一个**很小的** MLP（往往只有 1~2 个隐藏层）。
- **GPU / CUDA**：NVIDIA 显卡上的并行计算平台。instant-ngp 的训练和渲染全部在 GPU 上完成，这是它“快”的物理基础。
- **NeRF（Neural Radiance Field，神经辐射场）**：2020 年提出的一种用神经网络表示三维场景的方法。给定一个观察方向和空间位置，网络预测这里的颜色和密度，再通过“体渲染”把一条光线上的颜色合成成一个像素。instant-ngp 的“fox 示例”就是一个 NeRF。
- **SDF（Signed Distance Function，有向距离场）**：一种用“空间中某点到最近表面的有向距离”来描述几何形状的函数。距离为 0 的等值面就是物体的表面。

如果你对上面某个词完全陌生也没关系——本讲会用通俗的方式重新解释它们，更深入的原理会在后续单元（第四、五单元）展开。

---

## 3. 本讲源码地图

本讲主要阅读**文档与配置**，因为总览篇重在建立概念，而非阅读实现。涉及的关键文件如下：

| 文件 | 作用 | 本讲如何使用 |
| :--- | :--- | :--- |
| `README.md` | 项目的“门面”：定位、安装、用法、四种基元示例、论文链接、FAQ | 读取项目定位、四种基元的命令行示例、依赖说明 |
| `docs/nerf_dataset_tips.md` | NeRF 数据集准备与调参指南 | 佐证“秒级收敛”的训练特性、解释 `aabb_scale` |
| `include/neural-graphics-primitives/common.h` | 定义跨模块共享的枚举与类型 | 读取 `ETestbedMode` 枚举，证明“四种基元”在源码层面就是四种模式 |
| `configs/nerf/base.json` | NeRF 的默认网络配置 | 读取 `encoding` 块，看到真实的 `HashGrid` 参数 |

> 提示：本讲引用的链接都是**永久链接**（基于当前 HEAD `abe236ee`），点击即可在 GitHub 上跳转到对应代码行。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目背景**、**四种图形基元**、**多分辨率哈希编码思想**。

### 4.1 项目背景：instant-ngp 要解决什么问题

#### 4.1.1 概念说明

2020 年原始 NeRF 论文展示了惊人的效果——用一组二维照片就能重建出可自由旋转的逼真三维场景。但它有一个致命缺点：**训练慢**。原始 NeRF 在一块高端 GPU 上训练一个场景，通常需要**几十个小时**，渲染一张图也要几十秒。这让它在实际应用中几乎不可用。

instant-ngp（**Instant** Neural Graphics Primitives，“即时神经图形基元”）正是为解决这个问题而生。它的标题里带一个“Instant”，README 开篇第一句就把卖点讲透了：

[README.md:5-8](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L5-L8) —— README 开篇：把 NeRF 训练到 5 秒内、实现四种图形基元、用 tiny-cuda-nn 框架训练并渲染带多分辨率哈希编码的 MLP。

也就是说，同样一只狐狸的 NeRF，instant-ngp **几秒钟**就能训好，并且能实时（每秒数十帧）渲染。速度上的提升不是几倍，而是**几千倍**。这一突破来自 NVIDIA 的 Thomas Müller 等人，发表于 2022 年 SIGGRAPH，论文标题就是《Instant Neural Graphics Primitives with a Multiresolution Hash Encoding》：

[README.md:10-13](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L10-L13) —— 论文署名与出处（ACM Transactions on Graphics, SIGGRAPH 2022），以及项目主页/论文/视频链接。

需要特别理解的是：instant-ngp 的“快”并不是靠堆更多的算力或更大的网络，恰恰相反，它用的是**更小**的网络。关键在于**怎么把输入喂给网络**——也就是输入编码（input encoding）。这正是本讲的第三个模块要讲的“多分辨率哈希编码”。

#### 4.1.2 核心流程

可以用一句话概括 instant-ngp 的整体思路：

```
原始信号(位置/坐标)
        │
        ▼
   输入编码(把低维坐标映射成高维特征)   ←── 关键加速点：多分辨率哈希编码
        │
        ▼
       小型 MLP(1~2 个隐藏层)            ←── 网络很小，所以快
        │
        ▼
   预测值(颜色 / 距离 / 密度 ...)
        │
        ▼
   渲染(体渲染 / 球面追踪 / 直接查表)
```

这里的反直觉之处是：**让网络变简单，让编码变强**。传统做法是用一个又大又深的网络去“死记硬背”高频细节；instant-ngp 把记忆高频细节的工作交给一个**可学习的查找表（哈希表）**，网络只负责做轻量的组合。

#### 4.1.3 源码精读

instant-ngp 把“训练 + 渲染一个 MLP”这件事落地为一个可执行程序，而底层的神经网络能力来自一个独立的库 **tiny-cuda-nn**。README 的“致谢与开源依赖”部分明确列出了这一点：

[README.md:327-335](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L327-L335) —— 列举项目使用的开源库，其中第一条就是 `tiny-cuda-nn`（用于快速 CUDA 网络与输入编码）。

理解这条**职责边界**非常重要：

- **instant-ngp（本仓库）**：应用层。负责四种基元的“业务逻辑”——数据加载、训练循环、渲染管线、GUI、VR、导出网格等。
- **tiny-cuda-nn（外部依赖）**：库层。提供通用的 MLP、各种输入编码（含哈希编码）、优化器、损失函数，以及让它们在 GPU 上高速运行的 CUDA 内核。

当你后续想“改网络结构 / 改损失函数”时，往往要动的是 tiny-cuda-nn；而想“改 NeRF 的训练采样 / 渲染步进”时，动的是 instant-ngp。这条边界贯穿整本手册。

> 「秒级收敛」的佐证：数据集指南里有一句经验之谈——如果 NeRF 在大约 20 秒后看起来还没收敛，那它通常也不会再变好多少；几乎所有收敛都发生在最初几秒。这正是 instant-ngp 训练特性的真实写照。[docs/nerf_dataset_tips.md:10-14](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/docs/nerf_dataset_tips.md#L10-L14)。

#### 4.1.4 代码实践

> **实践目标**：用一句话写下 instant-ngp 与原始 NeRF 在训练速度上的差异，并确认你能在 README 里找到这个论断的出处。

**操作步骤**：

1. 打开本仓库的 [README.md](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md)。
2. 找到第 5 行那句 “Ever wanted to train a NeRF model of a fox in under 5 seconds?”。
3. 用你自己的话写一句话，描述“5 秒”这个数字相对于原始 NeRF（小时级训练）意味着什么。

**需要观察的现象**：你会注意到 README 几乎不解释“它是怎么做到这么快的”——这正是本讲后续模块和整本手册要回答的问题。

**预期结果**：你应当能写出一句话，例如：“原始 NeRF 训练一个场景要几十小时，instant-ngp 把它压缩到 5 秒量级，提升约数千倍。”

**待本地验证**：若你想亲自感受差异，需要在配好 NVIDIA GPU 的机器上编译运行（见 u1-l3），本篇暂不要求运行。

#### 4.1.5 小练习与答案

**练习 1**：instant-ngp 之所以快，主要原因是它用了更大的网络和更多的 GPU 吗？

> **参考答案**：不是。恰恰相反，它用的是更小的网络。加速的关键在**输入编码**——多分辨率哈希编码用一个可学习的哈希查找表承担了记忆高频细节的工作，从而让 MLP 可以做得很小、训练很快。

**练习 2**：本仓库（instant-ngp）和外部依赖 tiny-cuda-nn 各自负责什么？

> **参考答案**：instant-ngp 是应用层，负责四种基元的数据加载、训练、渲染、GUI 等“业务”；tiny-cuda-nn 是库层，提供通用的 MLP、输入编码、优化器、损失函数和高速 CUDA 内核。

---

### 4.2 四种图形基元：NeRF / SDF / 图像 / 体素

#### 4.2.1 概念说明

README 明确说，本项目实现了四种“神经图形基元”：

[README.md:7-8](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L7-L8) —— “四种神经图形基元：神经辐射场（NeRF）、有向距离函数（SDF）、神经图像、神经体素；每种都用带多分辨率哈希编码的 MLP 来训练和渲染。”

“图形基元（graphics primitive）”可以理解为“一种可以用神经网络表示、并能被渲染出来的图形对象”。四种基元的输入输出截然不同，但它们共享同一套底层机制（哈希编码 + 小 MLP）。下表概括它们的本质：

| 基元 | 输入 | 网络输出 | 表示什么 | 典型用途 |
| :--- | :--- | :--- | :--- | :--- |
| **NeRF**（神经辐射场） | 3D 位置 + 观察方向 | 颜色 + 密度 | 一个三维场景 | 从照片重建可旋转的 3D 场景 |
| **SDF**（有向距离场） | 3D 位置 | 到表面的有向距离 | 一个几何形状的表面 | 从网格学习光滑曲面、可球面追踪渲染 |
| **图像**（神经图像） | 2D 像素坐标 | RGB 颜色 | 一张（可超大）图片 | 用神经网络拟合/压缩一张图，含 gigapixel |
| **体素**（神经体素） | 3D 位置 | （主要来自 NanoVDB 体素） | 稀疏体素体积 | 渲染烟雾/云等体积数据 |

可以看到：NeRF、SDF 是“学习一个 3D 函数”；图像是“学习一个 2D 函数”；体素则更特殊（下一小节说明）。**同一个加速思想被套用在四种完全不同的任务上**——这正是这篇论文标题里“graphics primitives（复数）”的含义。

#### 4.2.2 核心流程

四种基元在程序里并不是四套独立的代码，而是**同一个程序（同一个 Testbed）的四种模式**。README 在 Usage 一节给每种基元都配了一个开箱即用的示例数据：

```
用户拖入一个文件（或用命令行指定）
        │
        ▼
程序根据文件类型自动判断模式          ←── 模式自动识别（详见 u2-l3）
        │
   ┌────┴────┬─────────┬──────────┐
   ▼         ▼         ▼          ▼
 data/      data/     data/      wdas_cloud_
 nerf/fox   sdf/      image/     quarter.nvdb
 (NeRF)    armadillo  albert.exr  (Volume)
           .obj (SDF)  (Image)
        │
        ▼
   进入对应的 ETestbedMode，开始训练 + 渲染
```

源码层面，这“四种模式”是一个枚举 `ETestbedMode`。在 `common.h` 中能看到它的定义，把 README 里的自然语言“四种基元”精确对应成了四个枚举值（外加一个 `None` 表示尚未选择模式）：

[include/neural-graphics-primitives/common.h:149-155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L149-L155) —— `ETestbedMode` 枚举：`Nerf`、`Sdf`、`Image`、`Volume`，以及 `None`。这是四种基元在源码中的“身份证”。

这是本讲第一次“接触源码”。你现在不需要理解这个枚举怎么被使用，只需记住：**整个 instant-ngp 程序的中心是一个叫 Testbed 的对象，它随时处于这五种状态之一**。后续单元（尤其 u2-l1）会深入剖析 Testbed。

#### 4.2.3 源码精读

README 的 Usage 章节为四种基元各写了一个最小示例命令。我们逐个对照阅读：

**① NeRF —— 狐狸**

[README.md:46-56](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L46-L56) —— NeRF fox 示例：`./instant-ngp data/nerf/fox`。这里传入的是一个**目录**（含 `transforms.json` 与若干照片），程序据此进入 `Nerf` 模式。

`data/nerf/fox/transforms.json` 是一组 50 张围绕狐狸拍摄的照片的相机参数文件，其中第 15 行的 `"aabb_scale": 4` 决定了场景包围盒的大小（含义见下文 4.2.5 与第四单元）。

**② SDF —— 犰狳**

[README.md:58-66](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L58-L66) —— SDF armadillo 示例：`./instant-ngp data/sdf/armadillo.obj`。这里传入的是一个 **`.obj` 三角网格**文件，程序进入 `Sdf` 模式，目标是学会这个网格表面的有向距离场。

**③ 图像 —— 爱因斯坦**

[README.md:68-82](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L68-L82) —— Image of Einstein 示例：`./instant-ngp data/image/albert.exr`。这里传入一张 **`.exr` 高动态范围图片**，程序进入 `Image` 模式，用坐标→颜色的回归拟合整张图。该节还提到可用 `scripts/convert_image.py` 把超大图转成 `.bin` 格式。

**④ 体素 —— 迪士尼云**

[README.md:85-92](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L85-L92) —— Volume renderer 示例：`./instant-ngp wdas_cloud_quarter.nvdb`。这里传入一个 **NanoVDB（`.nvdb`）稀疏体素**文件，程序进入 `Volume` 模式渲染体积数据。

注意一个小细节：前三个示例的数据 `data/nerf/fox`、`data/sdf/armadillo.obj`、`data/image/albert.exr` 都**随仓库一起提供**；而第四个 `wdas_cloud_quarter.nvdb` 需要你**自行下载**（README 给出了下载链接），所以本仓库里看不到它。

#### 4.2.4 代码实践

> **实践目标**：为四种基元各找出一个 `data/` 下的示例，理解“文件类型 → 基元模式”的粗略对应关系。

**操作步骤**：

1. 在仓库根目录浏览 `data/` 目录，确认存在：
   - `data/nerf/fox/`（目录，内含 `transforms.json` 与 `images/`）→ **NeRF**
   - `data/sdf/armadillo.obj`（三角网格）→ **SDF**
   - `data/image/albert.exr`（图片）→ **图像**
2. 体积（Volume）示例不在仓库内，记录下它的文件名 `wdas_cloud_quarter.nvdb` 及其格式 `.nvdb`。
3. 在 README 的四个 Usage 小节里找到对应的启动命令，把“示例数据 → 命令 → 模式”三者一一对应填入下表（留给你自己完成）：

   | 基元 | data/ 下的示例 | 启动命令 | ETestbedMode |
   | :--- | :--- | :--- | :--- |
   | NeRF | `data/nerf/fox` | `./instant-ngp data/nerf/fox` | `Nerf` |
   | SDF | `data/sdf/armadillo.obj` | ? | `Sdf` |
   | 图像 | `data/image/albert.exr` | ? | `Image` |
   | 体素 | （需下载）`wdas_cloud_quarter.nvdb` | ? | `Volume` |

**需要观察的现象**：你会看到“目录 / `.obj` / 图片 / `.nvdb`”四种不同的输入，分别触发四种模式。这说明程序是**根据文件后缀和类型自动判别**的（自动识别的源码细节在 u2-l3）。

**预期结果**：你能凭直觉说出粗略规则——目录或带相机参数的 → NeRF；三角网格 → SDF；单张图片 → 图像；`.nvdb` → 体素。

**待本地验证**：实际运行需要先编译（见 u1-l3）。

#### 4.2.5 小练习与答案

**练习 1**：NeRF 和 SDF 都输入 3D 位置，它们的网络输出有什么本质区别？

> **参考答案**：NeRF 输出的是**颜色和密度**（描述一个体场景，需要用体渲染合成像素）；SDF 输出的是**到最近表面的有向距离**（描述一个几何形状的表面，距离为 0 处就是表面）。前者表示“场景里有什么”，后者表示“形状的边界在哪”。

**练习 2**：为什么 `wdas_cloud_quarter.nvdb` 不在仓库的 `data/` 里，而其他三个示例都在？

> **参考答案**：因为该体积数据来自迪士尼的云数据集（CC BY-SA 3.0 授权），需要用户自行从外部链接下载；而 fox / armadillo / albert 体积小、可直接随仓库分发，所以已经放在 `data/` 下。

**练习 3**：在 NeRF fox 的 `transforms.json` 里有一个 `"aabb_scale"` 字段（fox 中为 4）。结合 [docs/nerf_dataset_tips.md:23-31](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/docs/nerf_dataset_tips.md#L23-L31)，简要说明它的作用。

> **参考答案**：NeRF 默认只在 `[0,0,0]` 到 `[1,1,1]` 的单位立方体内步进光线。`aabb_scale`（必须是 2 的幂，最大 128）把这个包围盒放大，使场景外的背景也能被重建。fox 设为 4，意味着光线会步进到一个边长为 4 的更大立方体里。

---

### 4.3 多分辨率哈希编码思想：核心创新

#### 4.3.1 概念说明

这是 instant-ngp 的**灵魂**，也是它名字里“Multiresolution Hash Encoding”的由来。理解了它，你才算真正理解了这个项目。

先说**为什么需要编码**。一个普通 MLP 直接吃 3D 坐标 `(x,y,z)` 时，有个致命弱点：它倾向于学习**平滑、低频**的函数。但真实世界充满了**高频细节**——毛发、纹理、锐利边缘。原始 NeRF 的解决办法是“位置编码”：把坐标用一堆正弦/余弦展开成高维向量再喂给网络，这能让网络学到高频，但代价是网络必须很大、训练很慢。

instant-ngp 的思路完全不同，它问：**与其让一个大网络去“算”出高频，为什么不直接用一张可学习的表去“查”出高频？**

这就引出了**多分辨率哈希编码**的三层思想：

1. **网格 + 查找表**。想象在空间里铺一层网格，每个网格顶点存一个小的可学习特征向量。要查询任意一点时，先找到它落在哪个网格里，再用网格 8 个顶点的特征做三线性插值，得到该点的编码。这些顶点特征就是网络要学习的参数。

2. **多分辨率**。一层网格分辨率太单一：粗网格抓不住细节，细网格又太稀疏。于是同时铺**很多层**分辨率从粗到细的网格（`n_levels` 层），每层各插值出一个特征，**全部拼起来**喂给 MLP。这样既能抓住大结构（粗层），又能抓住细节（细层）。各层分辨率按几何级数递增。

3. **空间哈希**。细层网格的顶点数量会爆炸性增长（一张高分辨率网格可能有上亿个顶点），不可能全存下来。解决办法是：用一个**固定大小的哈希表**（大小由 `log2_hashmap_size` 决定，例如 \(2^{19}\) 即约 52 万项）。多个顶点通过一个哈希函数映射到同一张表里——发生**哈希碰撞**时，不强行解决，而是让**梯度下降自己决定**哪处的特征更重要。碰撞处的特征会自然被“最需要它的任务”主导，其余地方因竞争失败而被忽略。这是整篇论文最巧妙的地方：**用训练动态来化解哈希冲突**。

#### 4.3.2 核心流程

把上面的思想画成流程：

```
输入点 x (3D 坐标)
   │
   ├── 层 0 (最粗分辨率)：查哈希表 → 插值 → 特征 f0
   ├── 层 1            ：查哈希表 → 插值 → 特征 f1
   ├── ...
   └── 层 L-1 (最细分辨率)：查哈希表 → 插值 → 特征 f_{L-1}
   │
   ▼
拼接 [f0, f1, ..., f_{L-1}]   ←── 高维、富含多尺度信息的输入编码
   │
   ▼
小型 MLP → 预测值
```

各层网格的分辨率按几何级数从 \(N_{\min}\)（`base_resolution`）增长到 \(N_{\max}\)（`desired_resolution`）。若共有 \(L\) 层，则相邻层的分辨率之比（即 `per_level_scale`）为：

\[
\text{per\_level\_scale} = \exp\!\left(\frac{\ln N_{\max} - \ln N_{\min}}{L - 1}\right)
\]

第 \(\ell\) 层的分辨率就是 \(N_\ell = \lfloor N_{\min} \cdot \text{per\_level\_scale}^{\ell} \rfloor\)。（这套公式在源码 `reset_network()` 里被自动推导，具体推导与对账留到 u3-l2，本讲只需理解它的几何级数含义。）

#### 4.3.3 源码精读

哈希编码的**算法实现**位于外部依赖 tiny-cuda-nn 中，本仓库不直接包含它的源码。但在本仓库里，你能直接看到**它的参数配置**。打开 NeRF 的默认配置 `configs/nerf/base.json`，`encoding` 块就是哈希编码的全部参数：

[configs/nerf/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L23-L29) —— `encoding` 配置块：`otype=HashGrid` 表示用多分辨率哈希网格编码，`n_levels=8` 表示 8 层分辨率，`n_features_per_level=4` 表示每层每顶点存 4 维特征，`log2_hashmap_size=19` 表示哈希表大小为 \(2^{19}\)，`base_resolution=16` 是最粗层分辨率。

逐个对照我们在 4.3.1 里讲的概念：

| 配置字段 | 含义 | 对应概念 |
| :--- | :--- | :--- |
| `otype: "HashGrid"` | 编码类型 | 多分辨率哈希编码 |
| `n_levels: 8` | 网格层数 | 多分辨率（8 个由粗到细的层） |
| `n_features_per_level: 4` | 每层每顶点的特征维数 | 拼接后的总编码维度 = \(8 \times 4 = 32\) |
| `log2_hashmap_size: 19` | 哈希表项数取 \(2^{19}\) | 固定大小的空间哈希表（约 52 万项） |
| `base_resolution: 16` | 最粗层分辨率 | 几何级数的起点 \(N_{\min}\) |

可以看到：**整个哈希编码的“容量”由这几个数字决定，而不是由网络大小决定**。这正是它能用一个小 MLP 表达高频信号的根本原因——高频信息被存在了哈希表里，MLP 只负责把多尺度特征组合成最终输出。

> 说明：本仓库还提供多种编码的对照配置（`configs/nerf/` 下的 `hashgrid.json`、`frequency.json`、`oneblob.json` 等），它们的对比实验在 u3-l4 展开。本讲只看默认的 `base.json`。

#### 4.3.4 代码实践

> **实践目标**：亲手算一次“拼接后的编码总维度”，并在配置里定位哈希表大小，建立对参数量级的直觉。

**操作步骤**：

1. 打开 [configs/nerf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json) 的 `encoding` 块（23–29 行）。
2. 计算：总编码维度 = `n_levels` × `n_features_per_level` = \(8 \times 4 = 32\)。
3. 计算哈希表项数：\(2^{\text{log2\_hashmap\_size}} = 2^{19} = 524288\) 项；每项存 4 维 float，故每层哈希表约占 \(524288 \times 4 \times 4\text{ 字节} \approx 8\text{ MB}\) 显存，8 层共约 64 MB。
4. 对比：同样是预测颜色，原始 NeRF 用的网络有 8 个全连接层、每层 256 个神经元；instant-ngp 这里 `network` 块只有 `n_neurons=64`、`n_hidden_layers=1`（见同文件 30–36 行）。

**需要观察的现象**：你会直观感受到“编码很重、网络很轻”的反差——instant-ngp 把参数预算主要花在了哈希查找表上，而不是堆叠全连接层。

**预期结果**：你应当能口算出 NeRF 默认编码输出 32 维、哈希表约 52 万项，并理解这 32 维的高维输入正是小网络也能学好高频的原因。

**待本地验证**：精确显存占用取决于 tiny-cuda-nn 的对齐与精度，上面只是量级估算。

#### 4.3.5 小练习与答案

**练习 1**：为什么哈希编码要用“很多层、从粗到细”的网格，而不是只用一层最细的网格？

> **参考答案**：一层最细网格的顶点数会爆炸（无法全部存储），且对粗大结构表达低效。多层从粗到细既能用粗层抓住大尺度结构、用细层抓住细节，又能在固定大小的哈希表预算下平衡表达力与开销。

**练习 2**：当多个网格顶点哈希到表的同一个槽位（碰撞）时，instant-ngp 怎么处理？

> **参考答案**：它不显式解冲突，而是让梯度下降自然决定：碰撞处的特征会被“训练中损失梯度最大、最需要它的那个任务”所主导，其他地方的贡献因竞争失败而被边缘化。也就是说，**用训练动态来化解哈希冲突**。

**练习 3**：在 `configs/nerf/base.json` 中，若把 `n_levels` 从 8 改成 16、`n_features_per_level` 保持 4，拼接后的编码总维度变成多少？这对网络输入有什么影响？

> **参考答案**：总维度变成 \(16 \times 4 = 64\)，翻倍。输入给 MLP 的特征向量更丰富、能表达更多细节，但同时也意味着更多可学习参数和更高显存/计算开销。这正是后续单元做“编码对比实验”（u3-l4）时要权衡的取舍。

---

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个“总览小任务”：

> **任务：用一张表向新人介绍 instant-ngp**

1. **定位**：阅读 [README.md:5-8](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L5-L8)，用一句话写出 instant-ngp 与原始 NeRF 在训练速度上的核心差异。
2. **四种基元**：参照 4.2，填写完成那张“基元 → data/ 示例 → 命令 → `ETestbedMode`”的对应表（参考 [README.md:46-92](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L46-L92) 的四个示例小节）。
3. **核心创新**：打开 [configs/nerf/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L23-L29)，用自己的话解释 `HashGrid` 的 `n_levels` / `log2_hashmap_size` / `base_resolution` 三个参数分别对应“多分辨率 / 哈希表 / 最粗层”的哪一层思想。
4. **边界**：写出 instant-ngp 与 tiny-cuda-nn 的职责分工（参考 [README.md:327-335](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/README.md#L327-L335)）。

**交付物**：一段 200 字以内的“项目介绍词” + 上面这张填好的对照表。如果某项你暂时无法确认（例如还没编译无法实际运行），请明确标注“待本地验证”，不要编造运行结果。

---

## 6. 本讲小结

- instant-ngp 的核心卖点是**把神经图形基元的训练从小时级压到秒级**，关键不是更大的网络，而是**更强的输入编码**。
- 它实现**四种图形基元**：NeRF（场景）、SDF（几何表面）、神经图像（2D 拟合）、神经体素（体积），在源码中对应 `ETestbedMode` 的 `Nerf / Sdf / Image / Volume` 四个枚举值。
- 四种基元共享同一套底层机制：**多分辨率哈希编码 + 一个很小的 MLP**；同一思想被套用到不同任务上。
- **多分辨率哈希编码**用“多层从粗到细的网格 + 固定大小哈希表 + 训练化解冲突”，把高频细节存进可学习的查找表，使网络可以很小却表达力很强。
- 本仓库（instant-ngp，应用层）与外部依赖（tiny-cuda-nn，库层）分工明确：哈希编码的算法实现在 tiny-cuda-nn，本仓库只提供配置（如 `configs/nerf/base.json`）和四种基元的业务逻辑。
- `data/` 下自带 fox / armadillo.obj / albert.exr 三个示例，分别对应 NeRF / SDF / 图像；体素示例 `wdas_cloud_quarter.nvdb` 需另行下载。

---

## 7. 下一步学习建议

本讲建立了“它是什么”的全局观。接下来建议按顺序：

1. **u1-l2 目录结构与代码组织**：走进 `src/` 和 `include/`，认识中枢文件 `testbed.cu` / `testbed.h`，理解为什么整个项目围绕一个 Testbed 类组织。
2. **u1-l3 构建与编译**：学会用 CMake 把项目编译成 `./instant-ngp` 可执行物与 `pyngp` Python 模块，这样你才能真正跑通本讲提到的四个示例命令。
3. **u1-l4 命令行运行与示例场景**：精读 `src/main.cu` 的入口与参数解析，亲手加载 fox / armadillo / albert / cloud 四种基元。

如果你急于深入算法本身，可以在学完 u1-l2 后直接跳到**第三单元（神经网络与多分辨率哈希编码）**，那里会从源码层面推导 `per_level_scale` 并做编码对比实验。但建议先按 1-2-3 的顺序把“能跑起来”这步走完。
