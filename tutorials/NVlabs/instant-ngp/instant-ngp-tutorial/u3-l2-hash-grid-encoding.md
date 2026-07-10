# 多分辨率哈希编码（核心创新）

## 1. 本讲目标

instant-ngp 之所以能把训练从「小时级」压到「秒级」，靠的不是更大的神经网络，而是一种叫**多分辨率哈希编码（multiresolution hash grid encoding）**的输入编码方式。它是整篇论文最核心的创新，也是 instant-ngp 的灵魂。

本讲学完后，你应该能够：

1. 说清楚**多层级网格**为什么能同时表达「低频大结构」和「高频小细节」。
2. 说清楚**空间哈希**如何用一个固定大小的哈希表去装下任意分辨率的网格顶点，以及在发生哈希碰撞时项目如何处理。
3. 看懂 `reset_network()` 里自动推导 `per_level_scale` 的那几行代码，并能手算出它的近似值。
4. 理解配置文件里 `n_levels`、`n_features_per_level`、`log2_hashmap_size`、`base_resolution`、`per_level_scale` 这五个参数各自的物理含义。

本讲承接 u3-l1：那里我们知道了 `reset_network()` 会消费 `m_network_config` 里的四大块配置（`encoding`/`network`/`optimizer`/`loss`）并把五大模型对象建出来；本讲我们就钻进 `encoding` 这一块，看那串「魔法参数」到底意味着什么。

> 说明：哈希编码的**算法实现**（真正的 CUDA 内核：哈希函数、三线性插值、梯度回传）位于外部依赖 `tiny-cuda-nn` 中，不在本仓库源码里。本仓库（应用层）的职责是：读取 JSON 里的编码参数、自动推导出 `per_level_scale`、把整理好的配置交给 tiny-cuda-nn 的 `create_encoding` 工厂去建网。所以本讲的源码精读会聚焦在「参数如何被解析与推导」，而非「哈希内核如何插值」。

## 2. 前置知识

在进入源码前，先用直白的话建立三个直觉。

**直觉一：为什么 MLP 需要输入编码？**

一个直接吃 3D 坐标 \((x,y,z)\) 的 MLP，本质是在用连续函数去拟合目标信号（颜色、密度、距离……）。但 MLP 倾向于学习**低频、平滑**的函数——这是谱偏置（spectral bias）。结果就是：大轮廓学得很快，细纹理、锐边缘怎么都学不出来。

解决办法是**编码**：把原始低维坐标先映射到一个高维、富含高频成分的特征空间，再喂给 MLP。原始 NeRF 用的是「位置编码」\(\gamma(x)=[x,\sin(2^0x),\cos(2^0x),\dots,\sin(2^{L-1}x),\cos(2^{L-1}x)]\)，固定、无参数。instant-ngp 的哈希编码则是**可学习**的：它用一个查找表去记忆高频信号。

**直觉二：网格插值是什么？**

想象在 \([0,1]^3\) 的单位立方体里铺一张分辨率为 \(N\) 的 3D 网格，每个网格顶点存 \(F\) 个可学习数（特征）。给定任意一点 \(x\)，找到它落在哪个网格单元里，用这个单元 8 个顶点的特征做**三线性插值**，就得到该点的 \(F\) 维特征。这本质上是一张「可学习的 3D 查找表」，对高频信号的拟合能力远强于裸 MLP。

**直觉三：网格越大越细越好，为什么不用超大网格？**

麻烦在顶点数量：一张 \(d\) 维、分辨率为 \(N\) 的稠密网格有 \(N^d\) 个顶点。3D 网格若想要 \(N=2048\)，就有 \(2048^3 \approx 8.6\times10^9\) 个顶点——显存直接爆炸。这正是哈希编码要解决的核心矛盾：**想要极高的分辨率，又不想付出稠密网格的显存代价。**

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/testbed.cu` | `reset_network()` 函数（约 L4160 起），其中 L4217–L4259 这一段专门解析编码参数并自动推导 `per_level_scale`。 |
| `include/neural-graphics-primitives/testbed.h` | 声明编码相关的成员变量 `m_n_levels`、`m_n_features_per_level`、`m_base_grid_resolution`、`m_per_level_scale`（约 L1080–L1084）。 |
| `configs/nerf/base.json` | NeRF 的默认编码配置：`HashGrid`，`n_levels=8`、`n_features_per_level=4`、`log2_hashmap_size=19`、`base_resolution=16`（L23–L29）。 |
| `configs/nerf/hashgrid.json` | 仅 `"parent": "base.json"`，本身不覆盖任何参数——它是「显式声明用哈希编码」的入口配置。 |
| `configs/sdf/base.json` | SDF 的默认编码配置：`n_levels=16`、`n_features_per_level=2`，可与 NeRF 对照阅读（L23–L29）。 |
| `data/nerf/fox/transforms.json` | NeRF 场景的 `aabb_scale`（fox 为 4，L14），它会进入 `per_level_scale` 的推导公式。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 多层级网格**：从粗到细堆叠多张网格。
- **4.2 空间哈希**：用固定大小哈希表存任意分辨率的顶点。
- **4.3 `per_level_scale` 推导**：几何级数如何刻画「最粗层到最细层的跨度」。

### 4.1 多层级网格：从粗到细逼近高频信号

#### 4.1.1 概念说明

只铺一张网格是不够的：太粗抓不住细节，太细又爆显存。多分辨率哈希编码的做法是**同时铺很多张分辨率不同的网格**。

设共有 \(L\) 层（对应配置里的 `n_levels`），第 \(\ell\) 层（\(\ell=0,1,\dots,L-1\)）的分辨率为

\[
N_\ell = \lfloor N_{\min}\cdot b^\ell \rfloor
\]

其中 \(N_{\min}\) 是最粗层分辨率（对应 `base_resolution`），\(b\) 是相邻两层的放大倍数（对应 `per_level_scale`）。于是第 0 层最粗、第 \(L-1\) 层最细，分辨率按**几何级数**递增。

对每个查询点 \(x\)：

1. 在每一层网格里分别做三线性插值，得到该层的 \(F\) 维特征（\(F\) 对应 `n_features_per_level`）。
2. 把 \(L\) 层的特征**拼接**起来，得到 \(L\cdot F\) 维的总特征向量。
3. 把这个总特征向量送进 MLP。

低层（粗）网格分辨率低、感受野大，负责刻画大尺度结构；高层（细）网格分辨率高、感受野小，负责刻画高频细节。两者拼接后一起喂给 MLP，模型就同时具备了「全局观」和「局部锐度」。

#### 4.1.2 核心流程

```
查询点 x (3D)
   │
   ├── 第 0 层 (N=N_min)       → 三线性插值 → F 维特征
   ├── 第 1 层 (N=N_min*b)     → 三线性插值 → F 维特征
   ├── ...
   └── 第 L-1 层 (N=N_min*b^(L-1)) → 三线性插值 → F 维特征
                                   │
                          拼接 L*F 维特征
                                   │
                                 送入 MLP
```

关键设计取舍：

- **层数 \(L\) 越多**，能覆盖的频率范围越广，但拼接后的特征维度 \(L\cdot F\) 也越大，MLP 输入越宽。
- **每层特征数 \(F\)** 越大，单层表达能力越强，但参数与计算量也线性增长。
- **放大倍数 \(b\)** 决定相邻层分辨率跳得多快——这正是模块 4.3 要推导的核心参数。

#### 4.1.3 源码精读

进入 `reset_network()` 后，当检测到编码类型是「网格类」时（`encoding_otype` 含 `grid` 或 `permuto`），代码会从 JSON 里提取这一组参数，并把它们存进 Testbed 的成员变量：

[src/testbed.cu:4217-4239](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4217-L4239) —— 检测到网格类编码后，提取 `n_features_per_level`、`n_levels`、`log2_hashmap_size`、`base_resolution`：

```cpp
// Automatically determine certain parameters if we're dealing with the (hash)grid encoding
std::string encoding_otype = to_lower(encoding_config.value("otype", "OneBlob"));
if (encoding_otype.find("grid") != std::string::npos || encoding_otype.find("permuto") != std::string::npos) {
    encoding_config["n_pos_dims"] = dims.n_pos;
    m_n_features_per_level = encoding_config.value("n_features_per_level", 2u);

    if (encoding_config.contains("n_features") && encoding_config["n_features"] > 0) {
        m_n_levels = (uint32_t)encoding_config["n_features"] / m_n_features_per_level;
    } else {
        m_n_levels = encoding_config.value("n_levels", 16u);
    }
    // ...
    const uint32_t log2_hashmap_size = encoding_config.value("log2_hashmap_size", 15);
    m_base_grid_resolution = encoding_config.value("base_resolution", 0);
    if (!m_base_grid_resolution) {
        m_base_grid_resolution = 1u << ((log2_hashmap_size) / dims.n_pos);
        encoding_config["base_resolution"] = m_base_grid_resolution;
    }
```

逐句说明：

- `m_n_features_per_level`：每层每个顶点存几个特征，默认 2。
- `m_n_levels`：层数。可以写 `n_levels` 直接给，也可以写 `n_features`（总特征数）让它除以 \(F\) 反推；都没有时默认 16。注意——**代码默认是 16，但 NeRF 的 `base.json` 会把它覆盖成 8**（见下文配置）。
- `log2_hashmap_size`：哈希表大小是 \(2^{\text{log2\_hashmap\_size}}\)，默认 15（即 32768）。这是模块 4.2 的主角。
- `m_base_grid_resolution`：最粗层分辨率 \(N_{\min}\)。若配置没给（值为 0），则自动从哈希表大小反推：\(2^{\lfloor\text{log2\_hashmap\_size}/d\rfloor}\)，\(d\) 是位置维度。对 3D、`log2_hashmap_size=19` 来说就是 \(2^6=64\)。

这些成员变量声明在头文件里：

[include/neural-graphics-primitives/testbed.h:1076-1086](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1076-L1086) —— 编码分析相关的成员变量，注释标了「Hashgrid encoding analysis」：

```cpp
// Hashgrid encoding analysis
std::vector<LevelStats> m_level_stats;
std::vector<LevelStats> m_first_layer_column_stats;
uint32_t m_n_levels = 0;
uint32_t m_n_features_per_level = 0;
uint32_t m_base_grid_resolution;
float m_per_level_scale;
```

而 NeRF 默认配置里这些参数的实际取值是：

[configs/nerf/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L23-L29) —— NeRF 默认编码：`HashGrid`，8 层、每层 4 特征、哈希表 \(2^{19}\)、最粗层分辨率 16：

```json
"encoding": {
    "otype": "HashGrid",
    "n_levels": 8,
    "n_features_per_level": 4,
    "log2_hashmap_size": 19,
    "base_resolution": 16
}
```

也就是说 NeRF 默认拼接出 \(8\times4=32\) 维的位置特征送进密度 MLP。

> 补充：`configs/nerf/hashgrid.json` 本身只有一行 `"parent": "base.json"`（[configs/nerf/hashgrid.json:1-3](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/hashgrid.json#L1-L3)）。它的意义不在于改参数，而在于**命名一个入口**——当你用 `--network hashgrid` 启动时，它通过 `parent` 继承机制拿到 `base.json` 的全部哈希编码参数。继承与合并的细节见 u2-l4。

#### 4.1.4 代码实践

**实践目标**：亲手验证「层数 × 每层特征数 = MLP 输入特征宽度」这条关系。

**操作步骤**：

1. 打开 `configs/nerf/base.json`，记下 `n_levels=8`、`n_features_per_level=4`。
2. 打开 `configs/sdf/base.json`，记下 `n_levels=16`、`n_features_per_level=2`（[configs/sdf/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L23-L29)）。
3. 分别算出两者的位置特征总宽度：NeRF \(8\times4=32\)，SDF \(16\times2=32\)。巧合但有意义——它们宽度相同，却用了完全不同的「粗细分层策略」。
4. 思考：NeRF 用更少但更宽的层，SDF 用更多但更窄的层，这对它们各自要表达的信号（NeRF 的颜色纹理 vs SDF 的光滑距离场）意味着什么？

**需要观察的现象**：两种模式编码参数不同，但最终送入 MLP 的特征宽度恰好都是 32 维。

**预期结果**：理解到「分层策略」和「总特征宽度」是两个独立的调节旋钮；`n_levels` 与 `n_features_per_level` 的乘积才是真正决定 MLP 输入宽度的量。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `n_levels` 从 8 加倍到 16、`n_features_per_level` 保持 4，MLP 输入特征宽度变成多少？哈希表会变大吗？

**答案**：输入宽度变为 \(16\times4=64\)（翻倍）。哈希表大小由 `log2_hashmap_size` 决定，**不会**因此改变——这正是哈希编码的精妙之处：加层只增加特征宽度，不增加（固定大小的）哈希表。

**练习 2**：为什么 NeRF 和 SDF 的 `base_resolution` 都设成 16，而不是 1？

**答案**：最粗层分辨率 \(N_{\min}\) 决定了「全局感受野」的粒度。\(N_{\min}=1\) 意味着整个空间只有一个顶点、完全无法分辨位置；16 能在最粗层就提供基本的空间区分度，作为细节层的合理起点。

---

### 4.2 空间哈希：用固定大小哈希表表达任意分辨率

#### 4.2.1 概念说明

模块 4.1 解决了「多分辨率」，但没解决「显存爆炸」：第 \(L-1\) 层分辨率为 \(N_{\min}\cdot b^{L-1}\)，对 3D 来说顶点数仍是 \((N_{\min}b^{L-1})^3\)，依然可能上亿。

**空间哈希**的思路是：**每层网格的顶点都存进一张固定大小为 \(T\) 的哈希表里**，\(T=2^{\text{log2\_hashmap\_size}}\)。

- 当某层的顶点数 \(\le T\) 时，直接一一对应存进去（**稠密索引**，无碰撞）。
- 当某层的顶点数 \(> T\) 时（典型是最细的几层），用哈希函数把整数坐标顶点映射到 \([0,T)\) 的表项：不同顶点可能落到同一个表项——这就是**哈希碰撞**。

哈希函数通常取「各坐标分量乘不同大质数后异或，再对 \(T\) 取模」的形式（实现细节在 tiny-cuda-nn，不在本仓库）：

\[
h(\mathbf{x}) = \left(\bigoplus_{i=1}^{d} x_i \cdot \pi_i\right) \bmod T
\]

其中 \(\pi_i\) 是一组大质数，\(\bigoplus\) 是按位异或，\(\mathbf{x}\) 是整数格点坐标。

#### 4.2.2 核心流程

碰撞发生时，多个空间位置共享同一组可学习参数。论文对碰撞的处理不是「想办法避免」，而是**让梯度下降来自动仲裁**：

```
两根不同位置的光线/查询点 x_a、x_b 哈希到同一个表项
        │
        ├── 若 x_a 处有真实信号（梯度大）→ 该表项被推向 x_a 需要的值
        ├── 若 x_b 处几乎无信号（梯度小）→ 对表项影响微弱
        │
   结果：重要的位置"抢到"了参数，不重要的位置被自然忽略
```

这叫「自组织」：哈希碰撞被训练过程**自然化解**——网络会把有限的参数预算花在信号强的区域（比如物体表面、纹理边缘），而空旷或平坦区域的碰撞因为梯度极小而被忽略。代价是：碰撞区域会有少量噪声/平均化，但因为多层叠加，相邻层往往能补偿，最终视觉效果几乎无损。

这就是 instant-ngp 能「参数量小、速度快、还高细节」的根本原因：用一张固定大小的表 + 碰撞竞争，近似替代了天文数字的稠密网格。

#### 4.2.3 源码精读

在 instant-ngp 这一层，哈希表大小由配置里的 `log2_hashmap_size` 决定。代码只是把它读出来、透传给 tiny-cuda-nn：

[src/testbed.cu:4233](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4233) —— 读取哈希表大小（以 2 为底的对数）：

```cpp
const uint32_t log2_hashmap_size = encoding_config.value("log2_hashmap_size", 15);
```

NeRF 与 SDF 的默认值都是 19，即哈希表有 \(2^{19}=524288\)（约 52 万）个表项：

- NeRF：[configs/nerf/base.json:27](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L27) —— `"log2_hashmap_size": 19`
- SDF：[configs/sdf/base.json:27](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L27) —— `"log2_hashmap_size": 19`

注意同一个常量在两个地方扮演不同角色：当 `base_resolution` 没给时，它还参与反推最粗层分辨率（见 4.1.3 的 `1u << (log2_hashmap_size / dims.n_pos)`）。

启动时，`reset_network` 会把这组整理好的参数打印一行摘要，方便你直接核对哈希表大小、层数等：

[src/testbed.cu:4257-4259](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4257-L4259) —— 打印 `MultiLevelEncoding` 摘要，含 `T=2^...`（哈希表大小）和 `L`（层数）：

```cpp
tlog::info() << "MultiLevelEncoding:"
             << " type=" << encoding_otype << " Nmin=" << m_base_grid_resolution
             << " b=" << m_per_level_scale
             << " F=" << m_n_features_per_level << " T=2^" << log2_hashmap_size
             << " L=" << m_n_levels;
```

> 说明：真正的「哈希函数 + 插值 + 碰撞处理」内核代码在 `dependencies/tiny-cuda-nn` 里（不在本仓库），本仓库只负责把 `log2_hashmap_size`、`n_levels` 等参数算好传过去。如果你想看哈希内核本身，需要去 tiny-cuda-nn 仓库的 `encoding.h`。

#### 4.2.4 代码实践

**实践目标**：通过修改 `log2_hashmap_size`，直观感受「哈希表大小」对质量与显存的影响。

**操作步骤**：

1. 复制一份配置：`cp configs/nerf/base.json configs/nerf/smallhash.json`。
2. 把 `smallhash.json` 里的 `"log2_hashmap_size"` 从 19 改成 15（哈希表从约 52 万缩到 3.2 万），其余不动。注意保持 `parent`/四大块结构完整。
3. 用 `./instant-ngp data/nerf/fox --network configs/nerf/smallhash.json`（或 pyngp 里 `testbed.reload_network_from_file(...)`）加载。
4. 观察 `MultiLevelEncoding` 那行日志里 `T=2^15`，以及训练若干秒后的画面清晰度与显存占用，与默认 `T=2^19` 对比。

**需要观察的现象**：哈希表变小后，高频细节（毛发、纹理）会更早出现「糊掉/串味」的碰撞噪声；显存占用下降。

**预期结果**：直观验证「哈希表是稀缺资源，太小则碰撞严重、细节丢失」。具体数值**待本地验证**（取决于 GPU 与场景）。

#### 4.2.5 小练习与答案

**练习 1**：为什么哈希碰撞在「空旷区域」几乎无害，而在「物体表面」必须被妥善处理？

**答案**：空旷区域没有真实信号，梯度接近零，碰撞共享的参数几乎不被更新，自然被忽略；而物体表面信号强、梯度大，多个表面点若碰撞到同一表项会互相「争夺」参数，处理不好就会出现伪影。正是因此训练时让梯度竞争来分配参数预算。

**练习 2**：把 `log2_hashmap_size` 加到极大（比如 30），会发生什么？

**答案**：哈希表几乎不再碰撞、质量上限提高，但显存按 \(2\) 的指数暴涨（\(2^{30}\) 项 × 每项 \(F\) 个浮点 × 层数），很快超出显存；而且参数过多反而拖慢训练。工程上 18–19 是常见甜点。

---

### 4.3 `per_level_scale` 推导：几何级数如何刻画粗细比

#### 4.3.1 概念说明

模块 4.1 留了一个关键参数没算：相邻两层的放大倍数 \(b\)（即 `per_level_scale`）。它决定了从最粗层 \(N_{\min}\) 到最细层 \(N_{\max}\) 这段跨度，**在 \(L\) 层之间如何分配**。

直觉上我们希望：

- 最粗层分辨率 \(=N_{\min}\)（由 `base_resolution` 给定，保证有基本空间分辨力）。
- 最细层分辨率 \(=N_{\max}\)（由「目标分辨率」给定，决定能表达的最高频率）。
- 中间各层按几何级数均匀铺开。

由 \(N_\ell=N_{\min}\cdot b^\ell\)，令最细层 \(\ell=L-1\) 等于 \(N_{\max}\)：

\[
N_{\max}=N_{\min}\cdot b^{L-1}
\]

解出 \(b\)：

\[
b=\left(\frac{N_{\max}}{N_{\min}}\right)^{\frac{1}{L-1}}=\exp\!\left(\frac{\ln(N_{\max}/N_{\min})}{L-1}\right)
\]

这就是 `per_level_scale` 的完整推导。它的几何意义是：在 \(L-1\) 个「台阶」里，把「最细/最粗」的比值 \(N_{\max}/N_{\min}\) 按**等比**分摊，每个台阶乘一次 \(b\)。

#### 4.3.2 核心流程

代码里需要先确定 \(N_{\max}\)（即 `desired_resolution`），再套上面的公式：

```
1. 取最细层目标分辨率 desired_resolution
   - 默认 2048.0（NeRF / SDF）
   - Image 模式：取图像最长边 / 2
   - Volume 模式：取 world2index_scale
2. 取最粗层分辨率 N_min = m_base_grid_resolution (base_resolution)
3. （仅 NeRF）N_max 还要再乘场景包围盒缩放 aabb_scale
4. b = exp( ln(N_max / N_min) / (L - 1) )
5. 若 JSON 显式给了 per_level_scale，则跳过自动推导，直接用
```

注意第 3 步：在 NeRF 模式下，\(N_{\max}\) 实际是 `desired_resolution * aabb_scale`。`aabb_scale` 来自数据集的 `transforms.json`，描述场景包围盒相对单位立方体的缩放——场景越大，最细层需要的格点数越多。

#### 4.3.3 源码精读

先看 `desired_resolution` 的取值分支：

[src/testbed.cu:4241-4246](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4241-L4246) —— 按模式确定最细层目标分辨率：

```cpp
float desired_resolution = 2048.0f; // Desired resolution of the finest hashgrid level over the unit cube
if (m_testbed_mode == ETestbedMode::Image) {
    desired_resolution = max(m_image.resolution) / 2.0f;
} else if (m_testbed_mode == ETestbedMode::Volume) {
    desired_resolution = m_volume.world2index_scale;
}
```

再看推导 `per_level_scale` 的核心几行：

[src/testbed.cu:4248-4255](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4248-L4255) —— 自动推导 `per_level_scale`，公式与本节推导完全一致：

```cpp
// Automatically determine suitable per_level_scale
m_per_level_scale = encoding_config.value("per_level_scale", 0.0f);
if (m_per_level_scale <= 0.0f && m_n_levels > 1) {
    m_per_level_scale = std::exp(
        std::log(desired_resolution * (float)m_nerf.training.dataset.aabb_scale / (float)m_base_grid_resolution) / (m_n_levels - 1)
    );
    encoding_config["per_level_scale"] = m_per_level_scale;
}
```

逐句对照本节公式：

- 代码里 \(N_{\max}=\)`desired_resolution * aabb_scale`，\(N_{\min}=\)`m_base_grid_resolution`。
- `std::log(...) / (m_n_levels - 1)` 就是 \(\ln(N_{\max}/N_{\min})/(L-1)\)。
- 外层 `std::exp(...)` 得到 \(b\)。
- 两个守卫条件：①若 JSON 已显式给了 `per_level_scale`（值 \(>0\)），就不自动推导，**直接用用户给的**；②`m_n_levels > 1`，因为只有一层时 \(L-1=0\) 会除零。
- 最后把算出的值写回 `encoding_config["per_level_scale"]`，确保下游 tiny-cuda-nn 的 `create_encoding` 能拿到。

关于 `aabb_scale`：对 NeRF 场景它来自 `transforms.json`，例如 fox 的值是 4：

[data/nerf/fox/transforms.json:14](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/data/nerf/fox/transforms.json#L14) —— `"aabb_scale": 4,`

#### 4.3.4 代码实践

**实践目标**：给定一组参数，手算 `per_level_scale` 并与代码对账。

**已知**：\(L=16\)（`n_levels`，取代码默认值）、\(N_{\min}=16\)（`base_resolution`）、`desired_resolution=2048`，并按本讲推导的抽象形式取 `aabb_scale=1`。

**手算过程**：

1. \(N_{\max}=2048\times1=2048\)。
2. 比值 \(N_{\max}/N_{\min}=2048/16=128\)。
3. \(\ln(128)=\ln(2^7)=7\ln2\approx7\times0.6931=4.8520\)。
4. 除以 \(L-1=15\)：\(4.8520/15\approx0.3235\)。
5. 取指数：\(b=\exp(0.3235)\approx1.382\)。

所以 `per_level_scale ≈ 1.382`。这正是公式 \(b=\exp\!\big(\ln(N_{\max}/N_{\min})/(L-1)\big)\) 的数值结果。

**与代码对账**：

- 若 `aabb_scale=1`，代码 L4251–L4253 计算的就是 `exp(log(2048/16)/15)`，与本手算**完全一致**，得到 `≈1.382`。
- 但真实 NeRF 场景里 `aabb_scale` 往往不是 1。以 fox 为例：`aabb_scale=4`、且 `base.json` 把 `n_levels` 覆盖成 8，于是代码实际算的是

\[
b=\exp\!\left(\frac{\ln(2048\times4/16)}{8-1}\right)=\exp\!\left(\frac{\ln 512}{7}\right)\approx\exp(0.891)\approx2.44
\]

  即 fox 默认配置下 `per_level_scale ≈ 2.44`（层数少、场景大，每层跨度自然更大）。**精确运行时数值待本地验证**——启动 fox 后查看 `MultiLevelEncoding` 那行日志里的 `b=` 字段即可核对。

**需要观察的现象**：启动 fox 后终端打印一行 `MultiLevelEncoding: ... Nmin=16 b=... F=4 T=2^19 L=8`，其中 `b` 即 `per_level_scale`。

**预期结果**：`aabb_scale=1`、`n_levels=16` 时 `b≈1.38`；fox 默认（`aabb_scale=4`、`n_levels=8`）时 `b≈2.44`。两者都可用本节公式精确复现。

#### 4.3.5 小练习与答案

**练习 1**：固定 `desired_resolution` 与 `base_resolution`，把 `n_levels` 从 16 翻倍到 32，`per_level_scale` 会变大还是变小？直觉如何？

**答案**：变小。分母 \(L-1\) 从 15 变 31，而分子 \(\ln(N_{\max}/N_{\min})\) 不变，所以每个台阶的放大倍数 \(b\) 更接近 1——也就是说层数越多，相邻层分辨率过渡越平滑、跳得越慢。

**练习 2**：为什么代码要加 `m_per_level_scale <= 0.0f` 这个判断？如果用户在 JSON 里写 `"per_level_scale": 1.5` 会怎样？

**答案**：这是「自动推导 vs 用户指定」的开关。用户显式给了正值就**尊重用户**、跳过推导；只有没给（取默认 0.0）时才自动算。写 `1.5` 会直接被采用——这给你一个高级旋钮：可以手动控制分层策略，而不必通过改 `n_levels` 间接影响。

**练习 3**：`aabb_scale` 变大时，`per_level_scale` 会怎样变化？为什么？

**答案**：变大。`aabb_scale` 进入分子 \(N_{\max}=\text{desired\_resolution}\times\text{aabb\_scale}\)，\(aabb\_scale\) 越大，\(N_{\max}/N_{\min}\) 越大，在层数不变的情况下每个台阶要跨更大的分辨率比，\(b\) 随之增大。这反映了「场景包围盒越大，固定层数内每层要覆盖更多空间」。

## 5. 综合实践

把三个模块串起来，做一次「配置驱动的对照实验」。

**任务**：复制 `configs/nerf/base.json` 为两份新配置 `configs/nerf/finer.json` 与 `configs/nerf/coarser.json`，分别把最细层目标分辨率（通过显式给定 `per_level_scale`）调高和调低，观察训练出的 fox 细节差异。

**建议步骤**：

1. **基线**：直接跑 `./instant-ngp data/nerf/fox`，记录启动日志里 `MultiLevelEncoding` 行的 `Nmin / b / F / T / L` 五个值，并截一张训练 5 秒后的图。
2. **更细**：在 `finer.json` 的 `encoding` 里加一行 `"per_level_scale": 1.6`（比 fox 默认的 ≈2.44 更平缓，意味着每层跨度更小、更偏向高频细分），其余继承 `base.json`。用 `--network configs/nerf/finer.json` 启动，同样记录日志与截图。

   > 注意：显式给 `per_level_scale` 会触发 L4250 的 `<= 0.0f` 判断为假，从而**跳过自动推导**（包括跳过 `aabb_scale` 的修正），直接用你给的 1.6。
3. **更粗**：在 `coarser.json` 里加 `"per_level_scale": 3.0`，重复实验。
4. **分析**：对照三张图与三个 `b` 值，回答——`b` 变小时细节是变锐还是变糊？哪一种在 fox 的毛发上表现更好？

**预期收获**：亲手验证「`per_level_scale` 控制粗细分层策略、进而控制高频表达力」这一核心结论，并理解自动推导（与 `aabb_scale` 联动）与手动指定（直接给定）两条路径的区别。

> 说明：本实践需要能编译运行的 instant-ngp 与一张支持 CUDA 的 NVIDIA 显卡；若本地无法运行，可改为「源码阅读型实践」：通读 [src/testbed.cu:4217-4259](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4217-L4259)，画出从 JSON 到 `m_per_level_scale` 的数据流图，标注每个参数的来源（JSON / 默认值 / `aabb_scale`）。

## 6. 本讲小结

- **多层级网格**：堆叠 \(L\) 层分辨率按几何级数递增的网格，每层插值出 \(F\) 维特征后拼接成 \(L\cdot F\) 维向量送入 MLP，让模型同时具备全局观与高频锐度。
- **空间哈希**：每层顶点存进固定大小 \(T=2^{\text{log2\_hashmap\_size}}\) 的哈希表；顶点数超过 \(T\) 时发生碰撞，靠梯度竞争「自组织」化解——这是「参数少、速度快、还高细节」的根本。
- **`per_level_scale` 推导**：\(b=\exp\!\big(\ln(N_{\max}/N_{\min})/(L-1)\big)\)，把「最细/最粗」比值在 \(L-1\) 个台阶里等比分摊。
- **代码落点**：这一切发生在 `reset_network()` 的 [src/testbed.cu:4217-4259](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4217-L4259)；`per_level_scale` 的自动推导在 [src/testbed.cu:4248-4255](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4248-L4255)。
- **aabb_scale 修正**：NeRF 模式下 \(N_{\max}\) 还要乘 `transforms.json` 里的 `aabb_scale`（如 fox=4），所以同一个公式在不同场景算出的 `b` 不同。
- **应用层 vs 库层边界**：本仓库只解析参数并推导 `per_level_scale`，真正的哈希内核（哈希函数、插值、碰撞回传）在 `tiny-cuda-nn` 中。

## 7. 下一步学习建议

- **横向对比编码**：本讲只讲了 HashGrid。建议读 u3-l4（编码方式对比实验），把 HashGrid、OneBlob、Frequency、DenseGrid、None 放在一起看，理解它们在参数量与表达力上的权衡，并用 `--network` 切换配置做对照实验。
- **纵向看网络**：哈希编码产出的特征最终喂给 MLP。建议读 u3-l3（网络构建与 FullyFusedMLP），理解为什么 `FullyFusedMLP` 要求 16 维对齐输入、`NetworkWithInputEncoding` 如何把编码与 MLP 打包。
- **去库层看内核**（进阶）：想真正看清「哈希函数 + 三线性插值 + 碰撞梯度回传」的 CUDA 实现，需要去 `dependencies/tiny-cuda-nn` 仓库读 `include/tiny-cuda-nn/encodings.h` 里的 `GridEncoding` 模板——那是本讲所有参数的最终消费者。
