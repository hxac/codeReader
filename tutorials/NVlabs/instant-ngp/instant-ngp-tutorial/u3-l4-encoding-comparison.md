# 编码方式对比实验

## 1. 本讲目标

本讲是「神经网络与多分辨率哈希编码」单元的收尾篇。前几讲我们已经知道：输入坐标要先进过一个**编码（encoding）**变成高维特征，再喂给 MLP；instant-ngp 默认用的是**多分辨率哈希编码（HashGrid）**，这是它「参数少、速度快、高细节」的根本。

但 tiny-cuda-nn 并不只提供 HashGrid 一种编码。本仓库的 `configs/nerf/` 目录里就放着好几份对照配置，让你**用同一份数据、换不同的编码**来训练，亲眼看出不同编码的差别。学完本讲，你应当能够：

1. 说出 **HashGrid / DenseGrid / Frequency / Identity** 四种编码各自的结构特点与参数来源。
2. 理解不同编码在**参数量**与**表达高频细节的能力**上的权衡。
3. 会用命令行 `-n` / `--network`（或 `-c` / `--config`）**切换配置**做受控对照实验。
4. 看懂源码里 `encoding` 的 `otype` 字段如何决定**是否触发自动参数推导**（如 `per_level_scale`）。

> 重要说明：本讲规格里提到 `configs/nerf/oneblob.json`，但**本仓库当前并不存在该文件**。`configs/nerf/` 下真实存在的编码对照配置是 `hashgrid.json`、`frequency.json`、`densegrid.json`、`none.json`（外加一份 `densegrid_1res.json`）。本讲只引用真实存在的文件；OneBlob 会作为「代码里的默认编码」顺带讲清，但不假装有对应的配置文件。

## 2. 前置知识

阅读本讲前，建议你已经掌握（参见前置讲义摘要）：

- **多分辨率哈希编码的三层思想**（u3-l2）：多层网格由粗到细各插值出特征、用固定大小的哈希表存顶点特征、靠梯度下降自然化解哈希碰撞。
- **JSON 配置四大块**（u2-l4）：`encoding` / `network` / `optimizer` / `loss`，以及 `parent` 继承与深度合并。
- **reset_network 的职责**（u3-l1）：从 `m_network_config` 取出四大块，按模式分发构造网络。
- **NeRF 走双头 NerfNetwork**（u3-l3）：位置先过 `pos_encoding` + 密度网络，方向再过 `dir_encoding` + 颜色网络。

几个本讲会用到的术语：

- **可学习参数（learnable parameters）**：训练过程中被优化器更新的数值。哈希表里的项是可学习参数；而 `sin`/`cos` 频率编码**没有**可学习参数（它是固定公式算出来的）。
- **对照实验（controlled experiment）**：只改一个变量（这里是编码），其余全部相同（同一数据、同一随机种子、同一训练步数），才能公平比较。
- **otype**：配置 JSON 里标明「用哪一种实现」的字符串字段，如 `"otype": "HashGrid"`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [configs/nerf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json) | NeRF 的**基线配置**，定义了默认的 HashGrid 编码（`n_levels=8`、`log2_hashmap_size=19`），是所有对照配置的 `parent` |
| [configs/nerf/hashgrid.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/hashgrid.json) | 仅继承 `base.json`，等于「显式选用默认 HashGrid」 |
| [configs/nerf/frequency.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/frequency.json) | 把编码换成 **Frequency**（频率编码），并把 MLP 换成更大的 `CutlassMLP 256×7` |
| [configs/nerf/densegrid.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/densegrid.json) | 把编码换成 **DenseGrid**（稠密网格，无哈希），并显式写死 `per_level_scale` |
| [configs/nerf/none.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/none.json) | 把位置编码与方向编码都设为 **Identity**（无编码），继承自 `frequency.json` 的大网络 |
| [src/main.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu) | 命令行入口，定义 `-n`/`--network` 等参数并调用 `reload_network_from_file` |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `reset_network` 里根据 `otype` 推导自动参数、统计参数量；`find_network_config` / `reload_network_from_file` 解析配置路径 |

## 4. 核心概念与源码讲解

### 4.1 四种输入编码的结构对比（编码对比）

#### 4.1.1 概念说明

NeRF 的输入是三维坐标（位置 `xyz`）和观察方向。MLP 本身很难直接从「裸坐标」还原出高频的纹理细节——坐标稍微动一点点，输出不该有剧烈跳变，但真实纹理恰恰需要高频跳变。**输入编码**就是插在「坐标」和「MLP」之间的一层映射，把低维坐标「展开」成高维特征，让 MLP 更容易学到高频信号。

instant-ngp 默认用 **HashGrid**，但 tiny-cuda-nn 还提供了别的编码。本仓库 `configs/nerf/` 下真实存在的四份对照配置分别对应：

| 编码 otype | 配置文件 | 直觉 | 可学习参数？ |
|---|---|---|---|
| `HashGrid` | `base.json` / `hashgrid.json` | 多层网格 + 哈希表存顶点特征（u3-l2 详讲） | 是（哈希表） |
| `DenseGrid` | `densegrid.json` | 多层网格，但**不用哈希**，把每一层的所有顶点都老老实实存下来 | 是（完整网格） |
| `Frequency` | `frequency.json` | 经典 NeRF 编码：\( \sin/\cos \) 的多组频率，无表格 | 否（解析计算） |
| `Identity` | `none.json` | **无编码**：坐标原样传给 MLP | 否 |

> 关于 **OneBlob**：它是高斯核网格编码，在源码里其实是 `otype` **缺省时的默认值**（见 4.3 节），但 `configs/nerf/` 下并没有 `oneblob.json`，所有 nerf 配置都显式写明了别的 `otype`。所以本讲不把它作为可运行对照项。

#### 4.1.2 核心流程

四种编码的关键差异，可以浓缩成两个问题：

1. **特征从哪里来？**
   - `HashGrid` / `DenseGrid`：从一张**可学习的查找表**里插值出来 → 训练要更新表里的数。
   - `Frequency`：用固定公式 \( \sin(2^k x), \cos(2^k x) \) 算出来 → 没有要学的表。
   - `Identity`：什么都不做。

2. **表格多大？**
   - `HashGrid`：表的大小由 `log2_hashmap_size` **固定**，跟网格多细**无关**（哈希碰撞靠梯度化解）。这是它省显存的关键。
   - `DenseGrid`：每一层要存**全部顶点**，顶点数随分辨率**立方增长**，所以很贵。

伪代码对比（仅示意，非项目代码）：

```
# HashGrid：固定大小哈希表，与分辨率无关
for level in 0..n_levels:
    h = spatial_hash(floor(x * res[level])) % TABLE_SIZE   # TABLE_SIZE = 2^log2_hashmap_size
    feat += trilinear_interp(table[level][h])

# DenseGrid：存全部顶点，随分辨率立方增长
for level in 0..n_levels:
    feat += trilinear_interp(full_grid[level])   # full_grid 大小 = res[level]^3

# Frequency：无表格，解析计算
for k in 0..n_frequencies:
    feat += [sin(2^k * x), cos(2^k * x)]
```

#### 4.1.3 源码精读

先看基线 `base.json` 里的 HashGrid 编码定义：

[configs/nerf/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L23-L29) —— 默认编码就是 `HashGrid`，`n_levels=8`、`n_features_per_level=4`、`log2_hashmap_size=19`、`base_resolution=16`。

而 `hashgrid.json` 只有一行继承：

[configs/nerf/hashgrid.json:1-3](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/hashgrid.json#L1-L3) —— `"parent": "base.json"`，所以它等价于「就用默认 HashGrid」。它的存在，主要是为了在命令行里显式、对称地选用编码（`--network hashgrid.json`）。

再看 `frequency.json`：

[configs/nerf/frequency.json:10-26](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/frequency.json#L10-L26) —— 编码改成 `Frequency`、`n_frequencies=16`；同时把网络换成更大的 `CutlassMLP`（256 神经元、7 隐藏层）。注释里作者点明：**在这个配置下，CutlassMLP 比 FullyFusedMLP 更快**——因为频率编码输出宽度（\( 3 + 2\times3\times16 = 99 \) 维）不再满足 16 对齐，全融合内核的优势没了（对齐细节见 u3-l3）。

接着是 `densegrid.json`：

[configs/nerf/densegrid.json:1-9](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/densegrid.json#L1-L9) —— 编码换成 `DenseGrid`，`n_levels=8`、`base_resolution=16`，并**显式写死** `per_level_scale=1.405`（不交给自动推导）。文件头注释说明：8 层从分辨率 16 到 173，参数量被刻意压到「不到 33M 的一半」——这正是稠密网格「立方爆炸」的反面教材：作者不得不手动控制层数与跨度，免得显存爆掉。

最后是 `none.json`：

[configs/nerf/none.json:1-9](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/none.json#L1-L9) —— 位置编码和方向编码都设为 `Identity`（无编码），继承自 `frequency.json`，于是仍保留那个大 MLP。这是最极端的对照：**没有任何编码，全靠大网络硬扛**，用来体会编码究竟带来了多少提升。

#### 4.1.4 代码实践

**实践目标**：用肉眼对比四种配置文件，建立「编码 = 输入到 MLP 之前的那层映射」的直觉。

**操作步骤**：

1. 打开 `configs/nerf/` 下 `base.json`、`frequency.json`、`densegrid.json`、`none.json` 四个文件。
2. 对每个文件，抄下它的 `encoding.otype` 和 `network` 部分（`n_neurons`、`n_hidden_layers`）。
3. 画出继承关系：`hashgrid.json → base.json`；`none.json → frequency.json → base.json`。

**需要观察的现象**：

- 越是「弱编码」，配置里把 MLP 调得越大（`frequency`/`none` 用 256×7；`hashgrid` 用 64×1）。这正暗示：编码弱了，就得拿更多神经元去补。

**预期结果**：你会得到一张这样的对照表（已为你填好）：

| 配置 | encoding.otype | network | n_neurons | n_hidden_layers |
|---|---|---|---|---|
| base / hashgrid | HashGrid | FullyFusedMLP | 64 | 1 |
| frequency | Frequency | CutlassMLP | 256 | 7 |
| densegrid | DenseGrid | FullyFusedMLP（继承） | 64 | 1 |
| none | Identity | CutlassMLP（继承） | 256 | 7 |

#### 4.1.5 小练习与答案

**练习 1**：`none.json` 没有写 `network` 字段，为什么它的 MLP 是 `CutlassMLP 256×7`？
**答案**：因为 `none.json` 的 `parent` 是 `frequency.json`，深度合并后继承了 `frequency.json` 里的大 `CutlassMLP`；它只覆盖了 `encoding`/`dir_encoding` 两个字段。

**练习 2**：为什么 `frequency.json` 用 `CutlassMLP` 而不是默认的 `FullyFusedMLP`？
**答案**：Frequency 编码输出约 99 维，不是 16 的倍数，无法满足 FullyFusedMLP 的 16 对齐要求（见 u3-l3）；作者在注释里也说这个配置下 CutlassMLP 反而更快。

**练习 3**：`densegrid.json` 为什么要把 `per_level_scale` 写死成 `1.405`？
**答案**：稠密网格参数量随分辨率立方增长，作者需要精确控制从 16 到 173 这 8 层的跨度，避免显存爆炸；写死数值也是为了实验可复现。

---

### 4.2 用 -n/--network 切换配置做对照实验（配置切换）

#### 4.2.1 概念说明

有了多份配置，怎么在运行时选用其中一份？instant-ngp 的命令行提供 `-n` / `--network`（别名 `-c` / `--config`）来指定网络配置文件。配合同一份场景数据，就能做受控对照实验：**只换配置、不换数据**。

这里有个细节：你可以写完整路径 `configs/nerf/frequency.json`，也可以只写文件名 `frequency.json`——`find_network_config` 会在 `configs/<模式>/` 下自动补全路径。这让「换模式时自动找到同名的对应配置」成为可能。

#### 4.2.2 核心流程

从命令行到网络重建的链路：

1. `main_func` 解析 `-n`/`--network` 参数（`network_config_flag`）。
2. 若指定了配置（且没有指定快照），调用 `testbed.reload_network_from_file(配置)`。
3. `reload_network_from_file` 用 `find_network_config` 把简写解析成完整路径，存下「用户传入的原始参数」（便于换模式时重定位），再真正读文件。
4. 读到的是 `.json` → 解析 + 合并 `parent` → 得到完整的 `m_network_config`。
5. 因为**不是快照**，紧接着调用 `reset_network()` 从零重建网络、训练步数 `m_training_step` 归零。

> 关键点：只要不是加载 `.ingp`/`.msgpack` 快照，换配置就一定会触发 `reset_network()`——也就是**从头训练**。这正是对照实验想要的：公平起跑线。

#### 4.2.3 源码精读

先看命令行参数定义：

[src/main.cu:50-55](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L50-L55) —— `network_config_flag` 的短选项是 `{'n', 'c'}`、长选项是 `{"network", "config"}`，所以 `-n`、`-c`、`--network`、`--config` 四种写法等价。

再看它在启动流程里的使用：

[src/main.cu:163-164](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/main.cu#L163-L164) —— 只有在**没有**指定快照时，才用 `--network` 重新加载配置；快照优先级更高（快照里已含训练好的权重）。

接着看路径解析：

[src/testbed.cu:254-270](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L254-L270) —— `find_network_config` 的三步：先看路径本身存不存在；绝对路径不补全；否则尝试 `根目录/configs/<当前模式小写>/<路径>`。注意目录名由 `to_string(m_testbed_mode)` 给出（nerf/sdf/image/volume），所以同名文件在不同模式下会指向不同配置。

最后看加载与重建：

[src/testbed.cu:311-344](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L311-L344) —— `reload_network_from_file` 先记录原始路径参数（第 319 行），再在模式已确定时读取配置；只要不是 `.msgpack` 快照，第 342 行就调用 `reset_network()` 从头重建。注释明确：加载非快照配置时「假定用户想看它从头训练」。

#### 4.2.4 代码实践

**实践目标**：跑通「同一数据 + 不同配置」的两条命令，确认配置确实被切换。

**操作步骤**：

```bash
# 在仓库根目录，先编译好 instant-ngp（见 u1-l3）

# 1) 默认 HashGrid
./instant-ngp data/nerf/fox --network configs/nerf/hashgrid.json --no-gui

# 2) 换成 Frequency（先按 Ctrl+C 停掉上一个）
./instant-ngp data/nerf/fox --network configs/nerf/frequency.json --no-gui
```

> `--no-gui` 让程序在命令行打印训练进度（无头模式，见 u1-l4）。`data/nerf/fox` 目录真实存在（含 `images/` 和 `transforms.json`）。

**需要观察的现象**：

- 启动日志里会先打印 `Loading network config from: ...`，路径就是你指定的那份配置。
- 紧接着会打印网络结构（见 4.3 节的参数统计行）。

**预期结果**：两条命令的「Loading network config from」路径不同；HashGrid 那条会多打印一行 `MultiLevelEncoding: type=hashgrid ...`，而 Frequency 那条**不会**打印这行——这正是下一节要讲的 `otype` 分支差异的肉眼证据。

> 提示：你也可以只写 `--network hashgrid.json`，`find_network_config` 会自动补全成 `configs/nerf/hashgrid.json`。但写完整路径最稳妥。

#### 4.2.5 小练习与答案

**练习 1**：`-n`、`-c`、`--network`、`--config` 四个写法有什么区别？
**答案**：没有区别，它们都绑定到同一个 `network_config_flag`（`src/main.cu:54` 的别名列表）。

**练习 2**：如果我同时给了 `--snapshot` 和 `--network`，会怎样？
**答案**：快照优先。`src/main.cu:163` 是 `else if (network_config_flag)`，只有未指定快照时才用网络配置；快照里已含权重，不需要再 reset。

**练习 3**：`--network frequency.json`（不带目录）为什么也能找到文件？
**答案**：`find_network_config`（`src/testbed.cu:264`）在路径本身不存在且非绝对路径时，会尝试 `<根>/configs/nerf/frequency.json` 并命中。

---

### 4.3 encoding otype 如何决定自动参数与参数量（参数权衡）

#### 4.3.1 概念说明

这是本讲最值得记住的一处源码细节：**不是所有编码都会被自动推导参数**。`reset_network` 里有一段「自动确定网格编码参数」的逻辑（计算 `per_level_scale`、补默认 `n_levels` 等），但它**只对名字里含 `grid` 或 `permuto` 的编码生效**。Frequency、Identity、OneBlob 都不走这条路。

为什么这样设计？因为 `per_level_scale` 这个概念本身只对**多层级网格**有意义——它描述「相邻两层分辨率之比」。频率编码没有「层」，自然不需要它。理解了这一点，你就能解释：为什么 `densegrid.json` 里 `per_level_scale=1.405` 会被代码直接采用，而 `frequency.json` 里压根没有这个字段。

同时，不同 `otype` 对应的**参数量**也天差地别，这正是「编码权衡」的核心：

- HashGrid：\( n_{levels} \times 2^{\log_2 hashmap\_size} \times n_{features\_per\_level} \)——**与最细分辨率无关**。
- DenseGrid：\( \sum_{l} res_l^3 \times n_{features\_per\_level} \)——**随分辨率立方增长**。
- Frequency：编码本身**无可学习参数**，参数全在 MLP 里。

#### 4.3.2 核心流程

`reset_network` 里处理编码的流程：

1. 读出 `encoding.otype`，**默认是 `OneBlob`**（即配置里不写 `otype` 时兜底）。
2. 判断 `otype` 是否含 `"grid"` 或 `"permuto"`：
   - **是**（HashGrid/DenseGrid/TiledGrid/Permuto…）：进入「多层级编码」分支，补默认 `n_features_per_level`、`n_levels`、`base_resolution`，并按需自动计算 `per_level_scale`，最后打印 `MultiLevelEncoding:` 日志。
   - **否**（Frequency/Identity/OneBlob…）：**跳过**整段自动推导，编码用各自特有的参数（如 Frequency 的 `n_frequencies`）。
3. 构造完编码与网络后，打印 `total_encoding_params` 和 `total_network_params`。

`per_level_scale` 的自动推导公式（与 u3-l2 一致）：

\[
b = \exp\!\left(\frac{\ln(N_{max}/N_{min})}{L-1}\right)
\]

其中 \(N_{min}\) 是 `base_resolution`，\(N_{max}\) 是「最细层期望分辨率」（NeRF 模式为 2048 × `aabb_scale`），\(L\) 是 `n_levels`。仅当配置未显式给出正的 `per_level_scale` 且 \(L>1\) 时才自动算。

#### 4.3.3 源码精读

先看默认值与分支判断：

[src/testbed.cu:4217-4219](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4217-L4219) —— `otype` 默认 `"OneBlob"`；只有当 `otype` 含 `"grid"` 或 `"permuto"` 时，才进入下面的自动参数块。这解释了为什么本讲规格假设的 `oneblob.json` 即便存在，也不会走 `per_level_scale` 推导。

再看自动参数补全与 `per_level_scale` 推导：

[src/testbed.cu:4248-4259](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4248-L4259) —— 先读配置里的 `per_level_scale`（缺省 0.0）；若 ≤0 且层数 >1，就用上面的公式自动算并写回配置；最后打印一行 `MultiLevelEncoding: type=... Nmin=... b=... F=... T=2^... L=...`。`densegrid.json` 里写死了 1.405，所以这段自动计算会被跳过，`b` 直接取 1.405。

最后看参数量统计与打印：

[src/testbed.cu:4379-4381](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4379-L4381) —— 用 `m_network->n_params()` 减去编码参数，分别得到 `total_network_params` 与 `total_encoding_params` 并打印。这是你做对照实验时读参数量的权威位置。

参数量的取数入口定义在这里：

[src/testbed.cu:4089-4091](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4089-L4091) —— `n_params()` 直接来自底层网络对象（tiny-cuda-nn）；`n_encoding_params()` 则是总参数减去首个编码器之前的层参数。

按公式估算各配置的编码参数量（**估算，以程序日志为准**）：

| 配置 | 编码参数估算 | 说明 |
|---|---|---|
| hashgrid | \(8 \times 2^{19} \times 4 \approx 16.8\)M | 哈希表固定大小 |
| densegrid | \(\sum_l res_l^3 \times 4\)（很大） | 立方增长，作者刻意压参数 |
| frequency | ≈ 0 | 无可学习编码参数，参数在大 MLP 里 |
| none | ≈ 0 | 无编码，全靠大 MLP |

#### 4.3.4 代码实践

**实践目标**：读出 hashgrid 与 frequency 两份配置的真实参数量，验证「哈希编码参数更少却更能逼近高频」。

**操作步骤**：

```bash
# 1) HashGrid：注意启动日志里的两行
./instant-ngp data/nerf/fox --network configs/nerf/hashgrid.json --no-gui
#    找 "MultiLevelEncoding:" 行 和 "total_encoding_params=... total_network_params=..." 行，抄下数字

# 2) Frequency（Ctrl+C 停掉上一个后）
./instant-ngp data/nerf/fox --network configs/nerf/frequency.json --no-gui
#    同样抄下 "total_encoding_params=... total_network_params=..." 行
```

**需要观察的现象**：

- HashGrid 启动时会打印 `MultiLevelEncoding: type=hashgrid Nmin=16 b=... F=4 T=2^19 L=8`；Frequency **不会**打印这行（因为它不是 grid 编码）——印证了 `src/testbed.cu:4219` 的分支判断。
- 两份配置的 `total_encoding_params` 与 `total_network_params` 截然不同：hashgrid 的编码参数是大头（约千万级），网络参数很小；frequency 反过来，编码参数≈0，网络参数（256×7 的大 MLP）是大头。

**预期结果**（具体数值**待本地验证**，这里给方向）：

- HashGrid：`total_encoding_params` 在千万量级（按公式约 16.8M），`total_network_params` 仅几万。
- Frequency：`total_encoding_params` 接近 0，`total_network_params` 在几十万量级（大 MLP）。

让两者各训练若干秒后比较最终损失（命令行持续打印的 `Loss=`）与肉眼清晰度：你会看到 **HashGrid 用更少（或相当）的总参数，反而能更快逼近毛皮等高频细节**——这就是多分辨率哈希编码的核心价值。

> 如果你想进一步体会「参数量随 `log2_hashmap_size` 指数变化」，可以对比 [configs/nerf/big.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/big.json)（`log2_hashmap_size=21`，表大 4 倍）、[configs/nerf/base_14.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base_14.json)（`=14`，表小 32 倍）与基线 19 的参数量差异。

#### 4.3.5 小练习与答案

**练习 1**：给定 `n_levels=8`、`log2_hashmap_size=19`、`n_features_per_level=4`，按公式估算 HashGrid 的编码参数量。
**答案**：\( 8 \times 2^{19} \times 4 = 8 \times 524288 \times 4 = 16\,777\,216 \approx 16.8\)M。实际值以程序日志 `total_encoding_params` 为准。

**练习 2**：为什么 `Frequency` 配置不会触发 `per_level_scale` 的自动推导？
**答案**：`src/testbed.cu:4219` 的判断条件是 `otype` 含 `"grid"` 或 `"permuto"`；`"frequency"` 都不含，所以跳过整段多层级自动参数块，也就没有 `per_level_scale` 这一概念。

**练习 3**：把 `log2_hashmap_size` 从 19 改成 21，编码参数量大约变成原来的几倍？
**答案**：哈希表大小翻 4 倍（\(2^{21}/2^{19}=4\)），其他不变，所以编码参数量约变为 4 倍。这正是 `big.json` 相对基线的变化。

## 5. 综合实践

**任务**：设计一张「编码 vs 参数量 vs 拟合质量」的对照表，亲手跑出数据。

要求：

1. 选定 fox 数据（`data/nerf/fox`）作为唯一场景，固定 `--no-gui`，保证对照公平。
2. 分别用四份配置各跑一次：`hashgrid.json`、`densegrid.json`、`frequency.json`、`none.json`。
3. 每次从启动日志抄下：`MultiLevelEncoding:` 行（若有）、`total_encoding_params`、`total_network_params`。
4. 让每个配置训练相同时间（例如都用 `Ctrl+C` 在大约相同秒数后停止，或用 `scripts/run.py --n_steps`，见 u7-l2），记录最后打印的 `Loss`。
5. 把结果填进下表，回答：哪种编码**总参数最少**却**损失最低/细节最好**？

| 配置 | otype | total_encoding_params | total_network_params | 总参数 | 训练末尾 Loss | 主观细节 |
|---|---|---|---|---|---|---|
| hashgrid | HashGrid | 待填 | 待填 | 待填 | 待填 | 待填 |
| densegrid | DenseGrid | 待填 | 待填 | 待填 | 待填 | 待填 |
| frequency | Frequency | 待填 | 待填 | 待填 | 待填 | 待填 |
| none | Identity | 待填 | 待填 | 待填 | 待填 | 待填 |

**预期结论**：HashGrid 通常以最节省的方式逼近高频细节；DenseGrid 也能逼近但参数/显存代价大得多；Frequency 需要大 MLP 才追得上；none（无编码）最难还原高频。这张表就是本讲全部内容的浓缩。

> 说明：精确数字依赖你的 GPU 与运行环境，请以本地实测为准；上表为「待本地验证」框架。

## 6. 本讲小结

- `configs/nerf/` 下真实的编码对照配置是 `hashgrid.json`、`frequency.json`、`densegrid.json`、`none.json`（外加 `densegrid_1res.json`）；**不存在 `oneblob.json`**，OneBlob 只是代码里 `otype` 缺省时的默认值。
- 四种编码的核心差异：HashGrid（固定哈希表，参数与分辨率无关）、DenseGrid（存全顶点，立方增长）、Frequency（解析 sin/cos，无编码参数）、Identity（无编码）。
- 用 `-n`/`-c`/`--network`/`--config` 切换配置；`find_network_config` 会把简写补全成 `configs/<模式>/` 下的完整路径；非快照配置加载后必然触发 `reset_network()` 从头训练。
- `reset_network` 里的自动参数推导（含 `per_level_scale`）**只对 `otype` 含 `grid`/`permuto` 的编码生效**；Frequency/Identity/OneBlob 都不走这条路。
- 参数量权威来源是启动日志的 `total_encoding_params` / `total_network_params`（`src/testbed.cu:4381`）。
- `log2_hashmap_size` 每加 1，HashGrid 哈希表翻倍、编码参数翻倍（`big.json`/`base_14.json` 即此例）。

## 7. 下一步学习建议

- 回到 **u4（NeRF 原语深入）**：编码选定后，下一步就是看 NerfNetwork 如何把位置编码喂给密度网络、把方向编码喂给颜色网络（u4-l2），以及训练循环如何采样光线（u4-l4）。
- 若对编码底层实现（哈希函数、三线性插值、碰撞梯度回传）感兴趣，本仓库已到边界——请移步依赖库 **tiny-cuda-nn** 的 `encoding.h`，那里是 HashGrid/DenseGrid/Frequency 的真正内核。
- 想做更多受控实验，可预习 **u7-l1（pyngp 绑定）** 与 **u7-l2（run.py）**：用 Python 脚本批量切换配置、记录损失曲线，比手动敲命令高效得多。
- 若要自定义编码或损失函数，参考 **u8-l5（扩展 instant-ngp）**：分清哪些改动在应用层（本仓库 configs）、哪些必须下沉到库层（tiny-cuda-nn）。
