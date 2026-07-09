# 网络配置体系：JSON 与继承

## 1. 本讲目标

在前几讲里，我们已经知道 `Testbed` 是整个项目的中枢，并且知道它持有的五个网络成员（`m_loss` / `m_optimizer` / `m_encoding` / `m_network` / `m_trainer`）会在 `reset_network()` 里被构造出来。但「这些网络对象到底按什么参数构造」这个问题，我们一直没有正面回答。

答案就是本讲的主角——**网络配置（network config）**。instant-ngp 把「这个场景用多大的哈希表、几层 MLP、什么损失函数、什么优化器」全部写进一个 JSON 文件，运行时读进来，再交给 tiny-cuda-nn 的工厂函数去建网。本讲学完后，你应该能够：

1. 打开任意一份 `configs/<mode>/base.json`，看懂 `encoding` / `network` / `optimizer` / `loss` 四大块各自在描述什么。
2. 理解 `"parent"` 字段带来的**配置继承与深度合并**机制，能预测一份「只写了几个字段」的子配置最终会展开成什么样。
3. 读懂 `find_network_config` 如何把一个简写的 `base.json` 解析成 `configs/nerf/base.json` 这样的真实路径，并理清 `reload_network_from_file` / `load_network_config` 的完整加载链路。
4. 区分三种「配置类」文件——`.json`（人类可读配置）、`.msgpack` / `.ingp`（二进制快照）——它们各自的加载方式与含义。

本讲只讲「配置如何被加载进内存」，至于「内存里的配置如何被 `reset_network()` 消费去建网」，留给下一单元 u3-l1。

## 2. 前置知识

- **JSON**：一种「键值对 + 嵌套对象 + 数组」的文本数据格式。instant-ngp 用 [nlohmann::json](https://github.com/nlohmann/json) 这个 C++ 库来解析它。你只需要知道 JSON 里的花括号 `{}` 表示一个对象（object），方括号 `[]` 表示数组（array），其余就是 `"键": 值`。
- **JSON 注释**：标准 JSON 不允许注释，但 nlohmann::json 在 `json::parse(..., true, true)`（第二个参数允许异常、第三个参数允许注释）模式下是支持 `//` 行注释的。instant-ngp 的配置文件里你会看到 `//` 注释，这是合法的。
- **merge_patch（深度合并）**：这是 [RFC 7396](https://datatracker.ietf.org/doc/html/rfc7396) 定义的一种合并规则，也是 `parent` 继承的核心。直觉上：把「子配置」一层层「叠」到「父配置」上——遇到同名键，若父子两边都是对象，就递归往里合并；否则子直接覆盖父。后文 4.2 会用真实例子讲透。
- **ETestbedMode 与 `to_string`**：在 u2-l1 我们讲过 `ETestbedMode` 有 `Nerf / Sdf / Image / Volume / None` 五个值。配置路径解析依赖一个把模式转成小写字符串的函数 `to_string(ETestbedMode)`，它返回 `"nerf" / "sdf" / "image" / "volume" / "none"`，正好与 `configs/` 下的四个子目录一一对应。

> 承接 u2-l3：那一讲我们讲的是「数据文件」如何被 `load_file` 路由到训练数据 / 快照 / 网络配置 / 相机路径四类。本讲我们zoom 进「网络配置」这一类，专讲它的内部结构、继承与加载。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `configs/nerf/base.json` | NeRF 模式的「基配置」，包含本讲要逐字段拆解的四大块，外加 NeRF 专属的 `dir_encoding` / `rgb_network` / `distortion_map` / `envmap`。 |
| `configs/sdf/base.json` | SDF 模式的「基配置」，结构与 NeRF 的 base 同源但参数不同，是本讲对比实践的对象。 |
| `configs/nerf/hashgrid.json` / `none.json` / `frequency.json` | 三份**只用 `parent` 继承**的子配置，用来演示继承链与深度合并。 |
| `src/testbed.cu` | 配置加载的全部实现都在这里：`merge_parent_network_config`（合并）、`find_network_config`（路径解析）、`load_network_config`（读盘解析）、`reload_network_from_file`（编排）。 |
| `src/common_host.cu` | `to_string(ETestbedMode)`，把模式枚举转成 `configs/` 下的目录名。 |
| `include/neural-graphics-primitives/testbed.h` | 声明上述加载函数，以及两个关键成员 `m_network_config_path`（配置路径）与 `m_network_config`（加载后的 JSON 对象）。 |

## 4. 核心概念与源码讲解

### 4.1 配置四大块：encoding / network / optimizer / loss

#### 4.1.1 概念说明

instant-ngp 的每一份网络配置，本质上是在回答四个问题：

1. **`encoding`（编码）**：怎么把输入坐标变成网络能吃的特征？这是本项目的核心创新（多分辨率哈希编码）。`HashGrid` 用一张可学习的哈希表，`Frequency` 用经典的正弦余弦频率编码，`OneBlob` 用核函数，`Identity` 则什么都不做。
2. **`network`（网络本体）**：用什么 MLP 把编码后的特征映射成输出？关键字段是 `otype`（`FullyFusedMLP` 全融合内核最快，`CutlassMLP` 更通用但慢一点）、`n_neurons`（每层神经元数）、`n_hidden_layers`（隐藏层数）、`activation`（隐藏层激活）。
3. **`optimizer`（优化器）**：用什么策略更新网络权重？instant-ngp 几乎总是用三层嵌套：`Ema`（梯度滑动平均）→ `ExponentialDecay`（学习率指数衰减）→ `Adam`（基础优化器）。
4. **`loss`（损失函数）**：用什么指标衡量预测与真值的差距？`L2`、`RelativeL2`、`Huber`、`MAPE`（平均绝对百分比误差）等。

> 直觉：一份配置 = 「编码器 + 网络 + 优化器 + 损失」四件套的采购清单。tiny-cuda-nn 拿到这份清单后，用各自的工厂函数（`create_encoding` / `create_network` / `create_optimizer` / `create_loss`）把对象new出来。这四件套的具体语义属于 tiny-cuda-nn 库（u3 系列会深入），本讲只关心「清单长什么样、怎么读进来」。

#### 4.1.2 核心流程

一份配置被读取后，四大块分别流向不同的工厂：

```
        m_network_config (一个 json 对象)
        ┌──────────┬──────────┬───────────┬──────────┐
        ▼          ▼          ▼           ▼          ▼
   "encoding"  "network"  "optimizer"  "loss"   (NeRF 专属: dir_encoding / rgb_network ...)
        │          │          │           │
        ▼          ▼          ▼           ▼
  create_encoding create_network create_optimizer create_loss   ← tiny-cuda-nn 工厂
        └──────────┴────┬─────┴───────────┘
                       ▼
                m_trainer (把它们绑成一个可训练的整体)
```

注意 NeRF 模式有额外的块：`dir_encoding`（方向编码，球谐函数）、`rgb_network`（颜色头 MLP），这是因为 NeRF 是双头结构（u4-l2 会详讲）。SDF / Image / Volume 模式只有基础四件套。

#### 4.1.3 源码精读

先看 NeRF 的基配置。**损失**块最简单，就一个类型：

[configs/nerf/base.json:2-4](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L2-L4) — NeRF 用 `Huber` 损失（对离群值更鲁棒，比 L2 增长慢）。

**优化器**块是三层嵌套，需要重点看懂：

[configs/nerf/base.json:5-22](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L5-L22) — 外层 `Ema`（`decay=0.95`，对梯度做指数滑动平均，平滑优化轨迹）→ 中层 `ExponentialDecay`（`decay_start=20000` 表示训练 2 万步后才开始衰减学习率，`decay_interval=10000` 表示每 1 万步衰减一次，`decay_base=0.33` 是每次乘的系数）→ 内层 `Adam`（基础学习率 `1e-2`，两个动量系数 `beta1=0.9 / beta2=0.99`）。这种「洋葱式」嵌套是 tiny-cuda-nn 用 `nested` 字段表达「包装器」的统一约定。

**编码**块是本项目的灵魂：

[configs/nerf/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L23-L29) — `HashGrid` 编码，`n_levels=8`（8 个从粗到细的网格层级）、`n_features_per_level=4`（每层输出 4 维特征）、`log2_hashmap_size=19`（哈希表大小取以 2 为底的指数：\(2^{19}=524288\) 个表项）、`base_resolution=16`（最粗层级的网格分辨率）。最终编码输出维度 = `n_levels × n_features_per_level` = \(8 \times 4 = 32\) 维。

**网络本体**块：

[configs/nerf/base.json:30-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L30-L36) — `FullyFusedMLP`（全融合内核，最快），隐藏层激活 `ReLU`，输出无激活（`None`），`n_neurons=64`，`n_hidden_layers=1`（一个隐藏层）。所以密度头 MLP 极小：输入 32 维 → 64 → 64 → 输出 1 维密度。这正是 instant-ngp 「秒级训练」的秘诀之一——网络非常小。

再对照 SDF 的基配置，损失和优化器都不一样：

[configs/sdf/base.json:2-4](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L2-L4) — SDF 用 `MAPE`（平均绝对百分比误差），因为 SDF 的目标是「有向距离」，量纲跨越很大（表面附近接近 0，远处很大），用百分比误差能让远近区域的损失量级可比。

[configs/sdf/base.json:5-22](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L5-L22) — SDF 的 Adam 基础学习率是 `1e-4`（比 NeRF 的 `1e-2` 小 100 倍），`ExponentialDecay` 的 `decay_start=10000 / decay_interval=5000`（更早、更频繁地衰减）。这反映了 SDF 训练更敏感、需要更保守的优化。

[configs/sdf/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L23-L29) — SDF 的 `HashGrid` 用 `n_levels=16 / n_features_per_level=2`，注意 \(16 \times 2 = 32\)，**编码总维度和 NeRF 一样都是 32**，但 SDF 选择了「更多层级、每层更少特征」（更细的几何分辨率），而 NeRF 选择了「更少层级、每层更多特征」。

> 这两个 `base.json` 是本讲综合实践的对比对象，先建立印象即可，5.1 会让你亲手把差异列成表。

#### 4.1.4 代码实践

**实践目标**：亲手读一份配置，验证你对四大块的理解，并体会「NeRF 模式多了哪些专属块」。

**操作步骤**：

1. 打开 `configs/nerf/base.json`，从上到下数一数顶层一共有几个键。
2. 打开 `configs/sdf/base.json`，同样数一遍。
3. 对照找出 NeRF 有、而 SDF 没有的键。

**需要观察的现象**：

- `configs/nerf/base.json` 的顶层键：`loss / optimizer / encoding / network / dir_encoding / rgb_network / distortion_map / envmap`（共 8 个）。
- `configs/sdf/base.json` 的顶层键：`loss / optimizer / encoding / network`（共 4 个）。
- 多出来的 `dir_encoding` / `rgb_network` / `distortion_map` / `envmap` 正是 NeRF 双头网络与相机自标定（u4-l2、u8-l3）专属的配置块。

**预期结果**：你会直观感受到「同一套四大块是所有模式共享的底座，每种模式再往上叠加自己的专属块」。这正是 `Testbed` 能用一份配置加载逻辑服务四种模式的设计基础。

**待本地验证**：若你想确认这些键名确实被代码读取，可在 `src/testbed.cu` 的 `reset_network()` 中搜索 `m_network_config.contains("dir_encoding")` 等调用（u3-l1 会精读）。

#### 4.1.5 小练习与答案

**练习 1**：`log2_hashmap_size=19` 时，哈希表实际有多少个表项？若改成 21 呢？

**答案**：表项数 \(= 2^{19} = 524288\)（约 52 万）。改成 21 则为 \(2^{21} = 2097152\)（约 210 万），翻 4 倍。这个值越大，哈希表越大、碰撞越少、能记住的细节越多，但显存占用也越高。仓库里 [configs/nerf/big.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/big.json) 用 21，[configs/nerf/small.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/small.json) 用 15，[configs/nerf/base_14.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base_14.json) 用 14，正是为不同显存预算准备的对照实验。

**练习 2**：为什么 instant-ngp 的 `optimizer` 几乎总是三层嵌套，而不是直接写一个 `Adam`？

**答案**：因为三个优化器各管一件事且可叠加——`Ema` 平滑梯度以稳定训练、`ExponentialDecay` 在训练后期自动降低学习率以精细收敛、`Adam` 提供带动量的基础更新。tiny-cuda-nn 用 `nested` 字段把它们像「俄罗斯套娃」一样组合，让你能自由替换任意一层（比如去掉衰减、换掉 Ema），而不必为每种组合写新优化器。

### 4.2 parent 继承与深度合并

#### 4.2.1 概念说明

如果每份配置都把四大块完整写一遍，会有两个问题：一是冗长易错（改一处要改 N 个文件），二是难以做对照实验（只想换一个编码方式，却要复制整份 base）。

instant-ngp 的解法是 **`parent` 继承**：一份子配置可以只写 `"parent": "base.json"` 加上**自己想改的几个字段**，加载时系统会自动把父配置读进来，再用子配置的字段去「覆盖/合并」。

关键的合并规则是 **深度合并（merge_patch）**：

- 若父子在某个键上**都是对象**，则递归地往里合并；
- 否则（任一方不是对象，或只在一方出现），**子直接覆盖父**；
- 子里出现、父里没有的键，直接补上；
- **唯独 `"parent"` 这个键本身在合并完成后会被丢弃**（它只是合并指令，不是网络参数）。

这套规则让你能精确地「只改最深处的一个参数」——例如只把嵌在最里层 `Adam` 的 `learning_rate` 从 `1e-2` 改成 `1e-3`，而保留整条优化器链的其他所有设置。

#### 4.2.2 核心流程

`parent` 合并是**递归**的，父配置自己也可以有 `parent`，从而形成继承链：

```
读子配置 child.json
  ├─ child 含 "parent": "frequency.json"?
  │     是 → 读 frequency.json 作为 parent
  │           ├─ parent 自身含 "parent": "base.json"?
  │           │     是 → 读 base.json 作为 grandparent  ← 递归
  │           │           (base 无 parent，原样返回)
  │           └─ parent.merge_patch(child)  ← 把 child 叠到 parent 上，丢掉 parent 键
  └─ 返回合并结果
```

最终结果是一份「摊平」的、不含任何 `parent` 键的完整配置。

#### 4.2.3 源码精读

合并的核心实现在这个自由函数里：

[src/testbed.cu:86-97](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L86-L97) — `merge_parent_network_config(child, child_path)`：第 87-89 行，若 `child` 没有 `"parent"` 键，直接原样返回（递归终止条件）；第 90 行用 `child_path.parent_path() / child["parent"]` 算出父配置的路径（**父路径是相对子配置所在目录解析的**，所以 `parent` 写相对路径即可）；第 93-94 行读入父配置，并**递归**调用自身去解析父配置的父配置；第 95 行 `parent.merge_patch(child)` 执行深度合并（`merge_patch` 是 nlohmann::json 内置的 RFC 7396 实现）；第 96 行返回合并结果。

注意一个细节：合并后并没有显式删除 `"parent"` 键——这是因为 `merge_patch` 的工作方式让最终对象里通常不会再保留它，且即便残留，`reset_network()` 也只关心四大块字段而忽略 `parent`。

来看三个真实例子，从最简单到最复杂。

**例子 1：纯继承，零覆盖。**

[configs/nerf/hashgrid.json:1-3](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/hashgrid.json#L1-L3) — 整份文件只有 `"parent": "base.json"`。加载后它就**完全等于** `base.json`（NeRF 的 base 本来就是 HashGrid 编码，所以这等价于显式选用 hashgrid）。它的存在是为了让 `--network hashgrid` 这样的命令行在语义上清晰。

**例子 2：单层继承 + 深度合并。**

[configs/nerf/frequency.json:1-34](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/frequency.json#L1-L34) — 继承 `base.json`，然后改了 `optimizer`、`encoding`、`dir_encoding`、`network`、`rgb_network`。重点看 [第 3-9 行的 optimizer](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/frequency.json#L3-L9)：子只写了 `"nested": { "nested": { "learning_rate": 1e-3 } }`。经过深度合并后，最内层 Adam 的 `learning_rate` 从 base 的 `1e-2` 被覆盖成 `1e-3`，而 `Ema` 的 `decay`、`ExponentialDecay` 的 `decay_start/interval/base`、Adam 的 `beta1/beta2/epsilon/l2_reg` **全部原样保留**。这就是深度合并的威力——你只写想改的那一片。

**例子 3：两层继承链。**

[configs/nerf/none.json:1-9](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/none.json#L1-L9) — 它的 `parent` 是 `frequency.json`，而 `frequency.json` 的 `parent` 又是 `base.json`，形成 `none → frequency → base` 三层链。`none.json` 把 `encoding` 和 `dir_encoding` 都改成 `Identity`（即不编码），其余全部继承自 `frequency.json`（大网络、CutlassMLP）。加载时 `merge_parent_network_config` 会递归先把 `frequency` 与 `base` 合并，再把 `none` 叠上去。

#### 4.2.4 代码实践

**实践目标**：用纸笔（或脚本）手动模拟一次深度合并，验证你对规则的理解。

**操作步骤**：

1. 取父配置 `configs/nerf/base.json` 的 `optimizer` 块（5-22 行）。
2. 取子配置 `configs/nerf/frequency.json` 的 `optimizer` 块（3-9 行）。
3. 手动执行 `merge_patch`：对每个键，父子都是对象就递归合并，否则子覆盖父。
4. 把合并后的 `optimizer` 写出来。

**需要观察的现象 / 预期结果**：合并后应为：

```
Ema (decay=0.95)                        ← 来自 base
 └ ExponentialDecay (decay_start=20000, decay_interval=10000, decay_base=0.33)  ← 来自 base
    └ Adam (learning_rate=1e-3, beta1=0.9, beta2=0.99, epsilon=1e-15, l2_reg=1e-6)
              ^^^^^^^^^^^^^^^^ 来自 frequency 覆盖    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ 来自 base 保留
```

只有 `learning_rate` 变成 `1e-3`，其余与 base 完全一致。如果你推出的结果和这个一致，就说明你掌握了 merge_patch。

#### 4.2.5 小练习与答案

**练习 1**：如果子配置里写 `"encoding": { "otype": "Frequency" }`（不带 `n_levels`），合并后 `encoding` 里会有 `n_levels` 吗？

**答案**：会。因为父（base）的 `encoding` 是对象，子的 `encoding` 也是对象，于是递归合并：子的 `otype` 覆盖父的 `otype`（变成 `Frequency`），而父的 `n_levels / n_features_per_level / log2_hashmap_size / base_resolution` 全部保留。这就是为什么 `frequency.json` 可以只写 `"encoding": { "otype": "Frequency", "n_frequencies": 16 }` 而不必重抄 base 的哈希参数——尽管 `n_frequencies` 是 Frequency 编码专用、`n_levels` 是 HashGrid 专用，合并时并不会互相冲突，只会让最终对象同时带上两套字段（多余的会被 tiny-cuda-nn 忽略）。

**练习 2**：`merge_parent_network_config` 是怎么找到 `frequency.json` 这个父文件的？如果父文件不存在会怎样？

**答案**：它用 `child_path.parent_path() / child["parent"]` 拼路径——即「子配置所在目录 + parent 字符串」。所以 `none.json` 里的 `"parent": "frequency.json"` 会在同目录（`configs/nerf/`）下找 `frequency.json`。若父文件不存在，第 92 行 `std::ifstream` 打开失败，第 93 行 `json::parse` 会抛异常，加载中止——所以 parent 路径写错会直接报错，不会静默退化。

### 4.3 路径解析与加载流程

#### 4.3.1 概念说明

用户（CLI 或 GUI）通常只给一个简写名，比如 `base.json` 或 `hashgrid`，但磁盘上的真实文件在 `configs/nerf/base.json`。`find_network_config` 负责把这层「简写 → 真实路径」的转换。它的解析规则是：

1. 如果传入的路径**本身已存在**，直接用它（说明用户给了完整或相对当前目录的有效路径）。
2. 否则，若路径是**绝对路径**（如 `/foo/base.json`），不再尝试解析，原样返回（绝对路径不存在的，就是不存在）。
3. 否则，尝试拼成 `<repo根目录>/configs/<模式小写>/<传入路径>`，若存在就用它——这是最常见的命中分支。
4. 都不行就原样返回（交给上层报错）。

其中 `<模式小写>` 就是 `to_string(m_testbed_mode)`，比如 NeRF 模式下是 `nerf`。

> 关键推论：同一份 `base.json` 在不同模式下会解析到不同文件（`configs/nerf/base.json` vs `configs/sdf/base.json`）。这就是为什么「切换模式时配置会自动跟着切」——`m_network_config_path` 存的是简写 `base.json`，切模式后 `find_network_config` 会拿新的 `m_testbed_mode` 重新解析。

加载流程还要区分两种来源：

- **`.json`**：人类可读文本配置，解析后需要做 `parent` 合并。
- **`.msgpack` / `.ingp`**：二进制快照（`.ingp` 是 zlib 压缩的 msgpack）。快照在**保存时就已经把 parent 摊平了**，所以加载时**不再做 parent 合并**。

#### 4.3.2 核心流程

从「用户给一个路径」到「`m_network_config` 被填好」的完整链路：

```
reload_network_from_file(path)                     ← 总编排
  ├─ 若 path 非空：find_network_config(path) 试解析
  │     └─ 存在 或 当前没有配置 → 把「简写 path」存进 m_network_config_path
  │        （注意：存的是简写，不是解析后的全路径！）
  ├─ 若 m_testbed_mode == None → 直接返回（模式未定，先不加载）
  ├─ full = find_network_config(m_network_config_path)   ← 再用当前模式正式解析
  ├─ 判定 is_snapshot：扩展名 == .msgpack？
  ├─ m_network_config = load_network_config(full)        ← 真正读盘+合并
  └─ 若不是 snapshot → reset_network()                    ← 用新配置重建网络
```

`load_network_config` 内部按扩展名分流：

```
load_network_config(path)
  ├─ 扩展名 .ingp → zlib 解压 + from_msgpack        ← 快照，parent 已摊平
  ├─ 扩展名 .msgpack → from_msgpack                  ← 快照，parent 已摊平
  └─ 扩展名 .json → json::parse + merge_parent_network_config  ← 文本配置，需合并
```

#### 4.3.3 源码精读

先看模式名是怎么变成目录名的：

[src/common_host.cu:176-185](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L176-L185) — `to_string(ETestbedMode)` 把枚举映射成小写串，`configs/` 的四个子目录名 `nerf/sdf/image/volume` 正是由它决定的。

再看路径解析：

[src/testbed.cu:254-270](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L254-L270) — `find_network_config`：第 255-257 行「原样存在就用」；第 260-262 行「绝对路径不再解析」；**第 264 行是核心**——`root_dir() / "configs" / to_string(m_testbed_mode) / network_config_path`，即 `<根>/configs/<模式>/<传入>`；第 265-267 行若存在则返回该候选；第 269 行兜底原样返回。

然后是两个 `load_network_config` 重载。流式版本用于从内存/快照流读：

[src/testbed.cu:272-278](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L272-L278) — `load_network_config(stream, is_compressed)`：压缩则套一层 `zstr::istream`（zlib 解压），再 `json::from_msgpack` 反序列化。

路径版本是主入口，按扩展名三分流：

[src/testbed.cu:280-309](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L280-L309) — 第 281-282 行先判定是否快照（`.msgpack` 或 `.ingp`）；第 283-287 行不存在则抛异常；第 292-301 行处理快照分支——`.ingp` 多套一层 `zstr::istream` 做 zlib 解压（见第 295-296 行注释），`.msgpack` 直接读，二者都用 `from_msgpack`，且**第 301 行注释明确说「假定快照里的 parent 已经被解析掉了」**；第 302-306 行处理 `.json` 分支——`json::parse`（第 304 行的 `true, true` 启用异常与注释支持）后**第 305 行调用 `merge_parent_network_config`** 完成继承合并。

最后是总编排 `reload_network_from_file`：

[src/testbed.cu:311-344](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L311-L344) — 第 312-321 行：若传了非空 `path`，先 `find_network_config` 探一下，命中（或当前没配置）就把**简写 path** 存进 `m_network_config_path`（注意第 316-318 行注释解释了为什么存简写而非全路径——为了切模式时能自动跟随）；第 325-327 行：模式还没定（None）就先不加载、只记住配置名；第 329 行用当前模式正式解析全路径；第 336 行调 `load_network_config` 填 `m_network_config`；**第 341-343 行**：只要不是快照，就调 `reset_network()` 重建网络（因为「换配置」通常意味着要从头训；加载快照则保留已训练权重）。

两个成员变量的声明在头文件里：

[include/neural-graphics-primitives/testbed.h:1224-1226](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1224-L1226) — `m_network_config_path` 默认值就是 `"base.json"`（所以不指定配置时，各模式默认加载各自的 `base.json`）；`m_network_config` 是加载后、合并后的完整 JSON 对象，供 `reset_network()` 消费。

构造函数也展示了三种入口：

[include/neural-graphics-primitives/testbed.h:73-82](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L73-L82) — 第 77-79 行的构造函数收一个 `network_config_path`（简写或全路径），转给 `reload_network_from_file`；第 80-82 行的构造函数直接收一个内存里的 `nlohmann::json` 对象，转给 `reload_network_from_json`（后者不做路径解析，但仍做 parent 合并，见 [src/testbed.cu:346-351](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L346-L351)）。

最后回到 u2-l3 提过的 `load_file`，看它如何把一个 `.json` 文件判定为「网络配置」：

[src/testbed.cu:384-389](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L384-L389) — 只要 JSON 含 `parent / network / encoding / loss / optimizer` 任一键，就认定它是网络配置，调 `reload_network_from_file`。这就是为什么本讲的「配置文件」能被自动识别。

#### 4.3.4 代码实践

**实践目标**：跟踪一次配置解析，验证 `find_network_config` 的「简写 → 全路径」转换与模式相关性。

**操作步骤**：

1. 假设仓库根目录为 `ROOT`，`m_testbed_mode == Nerf`，用户运行 `./instant-ngp --network hashgrid data/nerf/fox`。
2. 手动推演：`reload_network_from_file("hashgrid")` 被调用 → `find_network_config("hashgrid")` 执行。
3. 走 `find_network_config` 的三个分支，判断命中的是哪一条，最终 `m_network_config_path` 和实际加载的文件分别是什么。
4. 再假设 `m_testbed_mode == Sdf`，同样传入 `base.json`，推演命中文件。

**需要观察的现象 / 预期结果**：

- 传入 `"hashgrid"`：第 255 行 `exists()` 对无扩展名的 `hashgrid` 判否；非绝对路径；第 264 行拼成 `ROOT/configs/nerf/hashgrid`——但磁盘上是 `hashgrid.json`，所以这条也判否，第 269 行原样返回 `"hashgrid"`。最终 `m_network_config_path="hashgrid"`，加载时 `load_network_config("hashstack")` 找不到会报错。

  > **修正理解**：实际上 CLI 层通常传带扩展名的 `hashgrid.json`。此时第 255 行 `ROOT/configs/nerf/hashgrid.json` 经……注意 `exists()` 是对**传入字符串原样**判断，`hashgrid.json` 在当前工作目录通常不存在，于是走到第 264 行拼成 `ROOT/configs/nerf/hashgrid.json`，**命中**，返回全路径。这才是正确命中路径。`m_network_config_path` 存的仍是简写 `"hashgrid.json"`（见 4.3.3 第 316-318 行注释）。

- 传入 `"base.json"` 且模式为 Sdf：第 264 行拼成 `ROOT/configs/sdf/base.json`，命中 SDF 的 base。这验证了「同一名 `base.json` 在不同模式解析到不同文件」。

**待本地验证**：上述推演基于代码阅读，建议你在本地编译后用 `./instant-ngp data/nerf/fox --network hashgrid.json` 实跑，观察日志里 `Loading network config from: .../configs/nerf/hashgrid.json` 是否出现（对应 `load_network_config` 第 289 行的 `tlog::info()`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `reload_network_from_file` 在「不是快照」时才调 `reset_network()`，而加载快照时不调？

**答案**：因为快照（`.ingp` / `.msgpack`）里保存的是一个**已经训练好**的网络权重（连同它的配置）。加载快照的目的是恢复训练成果、直接渲染，所以不应该 `reset_network()` 把权重清零。而加载普通 `.json` 配置意味着「我要换一套网络结构从头训」，所以必须 `reset_network()` 重建。这正是第 339-343 行注释所说「presumably the network configuration changed and the user is interested in seeing how it trains from scratch」。

**练习 2**：`find_network_config` 为什么对绝对路径「直接返回、不再尝试拼 `configs/<mode>/`」？

**答案**：因为绝对路径（如 `/home/user/my.json`）已经是一个完整、明确的文件位置，再往前面拼 `configs/nerf/` 没有意义（会变成 `configs/nerf//home/user/my.json` 这种荒谬路径）。第 259-262 行注释明确说「The following resolution steps do not work if the path is absolute」，所以对绝对路径只做第 255 行的「存在性」检查，失败就原样返回让上层报错。

## 5. 综合实践

把本讲三大模块（四大块、parent 合并、路径解析）串起来，做一个完整的配置对比与改造任务。

### 5.1 对比 NeRF 与 SDF 的 base 配置

**任务**：对照 [configs/nerf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json) 与 [configs/sdf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json)，把差异填进下表（答案已在表中给出，请先自己读再核对）：

| 字段 | nerf/base.json | sdf/base.json | 含义 |
|------|----------------|---------------|------|
| `loss.otype` | `Huber` | `MAPE` | 损失函数类型 |
| `optimizer` Adam `learning_rate` | `1e-2` | `1e-4` | 基础学习率（SDF 小 100 倍） |
| `optimizer` ExponentialDecay `decay_start` | `20000` | `10000` | 学习率开始衰减的步数 |
| `optimizer` ExponentialDecay `decay_interval` | `10000` | `5000` | 衰减间隔 |
| `encoding.n_levels` | `8` | `16` | 哈希网格层级数 |
| `encoding.n_features_per_level` | `4` | `2` | 每层特征维度 |
| `encoding.log2_hashmap_size` | `19` | `19` | 哈希表大小指数（**两者相同**） |
| `encoding.base_resolution` | `16` | `16` | 最粗网格分辨率（相同） |
| `network.n_neurons` | `64` | `64` | 神经元数（相同） |
| `network.n_hidden_layers` | `1` | `2` | 隐藏层数（SDF 多一层） |

**关键洞察**：

1. **`log2_hashmap_size` 在两份 base 中其实是相同的（都是 19）**。它决定哈希表的表项数 \(2^{19}=524288\)。两份配置之所以在这里取相同值，是因为 19 对中小型场景是一个「性价比」不错的默认：足够大以减少碰撞，又不至于浪费显存。真正想观察 `log2_hashmap_size` 的影响，应改用 NeRF 自带的对照配置——`small.json`（15）、`base_14.json`（14）、`big.json`（21），它们才是为这个变量做的受控实验。
2. **两份配置真正在 `encoding` 上的差异是 `n_levels` 与 `n_features_per_level` 的「此消彼长」**：NeRF 是 \(8 \times 4 = 32\) 维，SDF 是 \(16 \times 2 = 32\) 维——**总维度一样，但分配策略不同**。SDF 要拟合精细的几何表面，所以用更多层级（更细的网格分辨率）换取空间细节；NeRF 要拟合颜色，所以每层给更多特征通道。这背后是 4.1 提到的 `per_level_scale` 自动推导（u3-l2 会深入）。
3. **`loss` 与 `optimizer` 的差异反映了任务性质**：SDF 的距离场量纲跨度大，用 `MAPE`（百分比误差）+ 小学习率 + 早衰减来稳住训练；NeRF 拟合的是像素颜色（量纲固定），用 `Huber` + 大学习率快速收敛。

### 5.2 改造一个子配置

**任务**：基于本讲学到的 parent 继承，亲手写一份只改两个参数的子配置。

1. 在 `configs/nerf/` 下新建 `mytest.json`（这是示例文件，**不要提交到仓库**，仅本地练习）。
2. 内容如下：

   ```json
   {
       "parent": "base.json",
       "optimizer": {
           "nested": {
               "nested": {
                   "learning_rate": 5e-3
               }
           }
       },
       "encoding": {
           "log2_hashmap_size": 21
       }
   }
   ```

3. 用 4.2 的 merge_patch 规则手算：合并后 `learning_rate` 变成 `5e-3`、`log2_hashmap_size` 变成 `21`，其余全部继承 base。
4. （待本地验证）用 `./instant-ngp data/nerf/fox --network mytest.json` 加载，观察日志是否打印 `Loading parent network config from: .../configs/nerf/base.json`（对应 `merge_parent_network_config` 第 91 行的 `tlog::info()`），以及显存占用是否因哈希表翻 4 倍而上升。

**预期结果**：你会看到 parent 被加载、合并发生，且训练初期的 loss 曲线因学习率变化而与 base 不同。这就完成了一次「零侵入」的超参实验——没有改动 base，也没有复制整份配置。

> 注意：本仓库的 worker 规则只允许写 `instant-ngp-tutorial/` 目录，所以 `mytest.json` 只能在你**自己 fork 的副本**里创建，不要写进本仓库。本讲义本身不创建该文件。

## 6. 本讲小结

- 一份网络配置 = **`encoding` + `network` + `optimizer` + `loss` 四大块**的采购清单；NeRF 模式额外有 `dir_encoding` / `rgb_network` / `distortion_map` / `envmap` 等专属块，SDF / Image / Volume 只有基础四件套。
- `optimizer` 几乎总是 `Ema → ExponentialDecay → Adam` 三层嵌套，用 `nested` 字段表达「包装器」组合；`log2_hashmap_size` 决定哈希表大小 \(2^{\text{log2\_hashmap\_size}}\)。
- **`parent` 继承**让子配置只写想改的字段：`merge_parent_network_config` 用 `parent.merge_patch(child)`（RFC 7396 深度合并）把子叠到父上，且递归支持多层继承链（如 `none → frequency → base`）。
- **路径解析** `find_network_config` 把简写（如 `base.json`）解析成 `<根>/configs/<模式小写>/<名>`；同一简写在不同模式解析到不同文件，这是「切模式自动切配置」的根源；`to_string(ETestbedMode)` 提供模式→目录名映射。
- **加载链** `reload_network_from_file` 编排全流程：存简写路径 → 模式未定则暂不加载 → `load_network_config` 读盘（`.json` 走 parse+merge，`.ingp` 走 zlib+msgpack，`.msgpack` 走 msgpack）→ 非 snapshot 则 `reset_network()` 重建网络。
- **`.json` 是文本配置**（需 parent 合并、加载后会重建网络从头训）；**`.ingp` / `.msgpack` 是二进制快照**（parent 已摊平、加载后保留已训练权重不 reset）。

## 7. 下一步学习建议

- **紧接 u3-l1（tiny-cuda-nn 模型对象与 reset_network）**：本讲我们止步于「`m_network_config` 被填好」，下一讲会精读 `reset_network()` 如何消费这份 JSON——用 `create_loss / create_optimizer / create_encoding / create_network` 把四大块变成真实的 C++ 对象，并解释 `LOSS_SCALE` 的混合精度原理。这是本讲的天然续篇。
- **u3-l2（多分辨率哈希编码）**：本讲多次提到 `n_levels / log2_hashmap_size / per_level_scale`，下一讲会给出 `per_level_scale = exp(log(Nmax/Nmin)/(L-1))` 的完整推导与代码。
- **u3-l4（编码方式对比实验）**：本讲 4.2 的三份子配置（hashgrid/frequency/none）正是为对照实验设计的，u3-l4 会带你用同一数据跑它们、对比参数量与拟合质量。
- **u6-l3（快照）**：本讲区分了 `.json` 与 `.ingp`/`.msgpack`，u6-l3 会完整讲快照的保存与加载，包括 `include_optimizer_state` 等细节。
