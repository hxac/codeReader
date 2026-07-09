# NerfNetwork 双头架构

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 NeRF 为什么要把网络拆成「密度头」和「颜色头」两段 MLP；
- 指出 `NerfNetwork` 的四个核心组件 `pos_encoding / density_network / dir_encoding / rgb_network`，并对应到 `configs/nerf/base.json` 的四块配置；
- 读懂 `forward_impl` 里「密度特征被零拷贝复用到颜色网络输入」的关键技巧；
- 解释 `density()` / `density_forward()` 这两个「只算密度」的专用接口为何存在，以及它们被密度网格、网格优化流程如何调用。

本讲承接 u3-l2（哈希编码）与 u3-l3（`FullyFusedMLP` 对齐），把视角从「单个 MLP 怎么造」升到「NeRF 怎么把两个 MLP、两套编码拼成一个端到端的网络对象」。

## 2. 前置知识

### 2.1 NeRF 体渲染要算什么

体渲染（volume rendering）的核心思想是：从相机出发向每个像素发一根光线，沿光线采样若干个 3D 点，每个点需要两个量——

- **体密度** \(\sigma\)：表示「这里有多不透明」，只和**位置** \(\mathbf{x}\) 有关（哪里有物质）；
- **颜色** \(c\)：表示「从这里向观察者发出什么色光」，既和**位置** \(\mathbf{x}\) 有关，也和**观察方向** \(\mathbf{d}\) 有关（同一物点从不同角度看，高光、镜面反射不同）。

最后把所有采样点的 \((\sigma, c)\) 用 alpha 合成（transmittance 累积）拼成像素颜色（u4-l3 会详讲）。

正因为密度和颜色依赖的输入不同，原始 NeRF 论文把网络设计成**两段式**：先用位置算密度，再把密度特征和方向一起喂给颜色网络。`NerfNetwork` 就是这个两段式结构的代码实现。

### 2.2 已经熟悉的积木

- **编码 + MLP**：u3-l1/u3-l2 讲过，位置先过 `Encoding`（如 `HashGrid`）变成高维特征，再喂 `Network`（MLP）。
- **`NetworkWithInputEncoding`**：u3-l3 讲过，它把「一个编码 + 一个 MLP」打包成一个网络对象，是 SDF/Image/Volume 三种模式的「单头」网络。
- **`FullyFusedMLP` 的 16 对齐**：u3-l3 讲过，全融合内核要求输入宽度是 16 的倍数。

`NerfNetwork` 可以理解为「`NetworkWithInputEncoding` 的双头升级版」：它有**两组**编码 + **两个** MLP，并且额外复用了 u3-l3 提到的 `NetworkWithInputEncoding` 作为内部积木。

### 2.3 显存布局术语（简单了解）

代码里频繁出现 `GPUMatrixDynamic`、`AoS`（Array of Structures，行主序）、`CM`（Column Major，列主序）、`slice_rows`（取若干行的「视图」）。你只需记住一点：`slice_rows` 不复制数据，而是给同一块显存换一个「看的位置和步长」，是本讲「密度特征复用」的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/neural-graphics-primitives/nerf_network.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h) | `NerfNetwork` 类的**全部实现**（声明与内联实现都写在这个头文件里），是本讲主角 |
| [configs/nerf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json) | NeRF 默认网络配置，能看到 `encoding / network / dir_encoding / rgb_network` 四块 |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `reset_network()` 中**构造** `NerfNetwork` 并接到 `m_network`/`m_nerf_network` 的地方 |
| [src/testbed_nerf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu) | 调用 `density()` 做密度网格批量采样、做网格优化的地方 |

> 提示：`NerfNetwork` 是**模板类**（`template <typename T>`，`T` 即 `network_precision_t`，混合精度下为半精度），全部代码都在头文件里，没有对应的 `.cu`。

---

## 4. 核心概念与源码讲解

### 4.1 双头 MLP：为什么要拆成密度头与颜色头

#### 4.1.1 概念说明

如果用一个大 MLP 同时输入「位置 + 方向」、直接输出「RGB + 密度」，会有两个问题：

1. **表征效率低**：密度只依赖位置，却被迫和方向纠缠在一起，网络难学好。
2. **计算浪费**：体渲染要在很多点上反复查密度（决定光线该不该继续走），而颜色只在最终着色点才算。

`NerfNetwork` 的解法是把网络拆成两段，文件头注释一句话点明了设计意图：

> A network that first processes 3D position to density and subsequently direction to color.
> （一个先处理 3D 位置得到密度、再处理方向得到颜色的网络。）

- **密度头**：`位置 → pos_encoding → density_network → 密度特征`（默认 16 维）。这 16 维既是「密度特征」，也会**原样**作为颜色头的输入之一。
- **颜色头**：`[密度特征 ‖ 方向特征] → rgb_network → RGB`（3 维）。

最终密度值由密度特征的某一维经过 `density_activation`（指数）得到（u4-l5 会讲）；颜色由 `rgb_network` 直接输出。

#### 4.1.2 核心流程

一次完整前向（输入是一条光线上的若干采样点，每个点带位置、方向等）：

```
输入 input = [ 位置(3) | dt(1) | 方向(3) | extra_dims ]
      │
      ├── pos_encoding(位置)        ──► 位置特征
      │        │
      │        └──► density_network ──► 密度特征(16维)  ──┐
      │                                    （第0维→密度） │ 复用
      ├── dir_encoding(方向+extra) ──► 方向特征         │
      │                                                   ▼
      └──────────────────────────────► rgb_network([密度特征 ‖ 方向特征]) ──► RGB(3维)

输出 output = [ R, G, B, density ]   ← 4 维 (RGBD)
```

注意输入里那个 `dt(1)`：它是光线步进的步距，**不参与网络计算**，只是和位置/方向紧凑排布在同一个结构体 `NerfCoordinate` 里，因此方向向量在输入张量中的起始偏移是「位置 3 + dt 1 = 4」——这个偏移很关键，下文会看到。

#### 4.1.3 源码精读

设计意图见文件头注释：

[include/neural-graphics-primitives/nerf_network.h:L11-L14](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L11-L14) — 一句话点明「先位置得密度、再方向得颜色」的两段式设计。

最直观地展现这条数据流的，其实是 **JIT 设备函数**（运行时编译，u8-l2 详讲）的生成代码——它把四个组件拼成一个端到端的 GPU 设备函数，body 部分读起来几乎就是上面的伪代码：

[include/neural-graphics-primitives/nerf_network.h:L490-L499](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L490-L499) — `generate_device_function` 的函数体：先 `pos_enc → density_mlp`，把密度特征放进 `rgb_mlp_in` 的前若干维，再 `dir_enc` 填后半段，最后 `rgb_mlp` 输出 RGB，并把 `rgb_mlp_in[0]`（即密度特征第 0 维）作为返回值的第 4 个分量。

返回的 `{rgb_mlp_out[0], rgb_mlp_out[1], rgb_mlp_out[2], rgb_mlp_in[0]}` 正是 `[R, G, B, density]`，与「输出 4 维 RGBD」一致——这也是为什么本讲标题强调「密度值被复用」。

#### 4.1.4 代码实践

1. **目标**：用 JIT 设备函数体确认双头数据流。
2. **步骤**：打开 `nerf_network.h` 第 490–499 行，对照 4.1.2 的伪代码，逐行标注每个占位符（`POS_ENC`、`DENSITY_MLP`、`DIR_ENC`、`RGB_MLP`）对应哪个组件。
3. **观察**：注意 `rgb_mlp_in.slice<0, {DENSITY_MLP_DIMS_OUT}>()` 和 `rgb_mlp_in.slice<{DENSITY_MLP_DIMS_OUT}, {DIR_ENC_DIMS_OUT}>()` 这两行——它们把同一个 `rgb_mlp_in` 向量的「前半段」给密度特征、「后半段」给方向特征。
4. **预期结果**：你能说清「密度特征占了输入的前 16 维、方向特征占后面」这一布局约定，这正是下一节 `forward_impl` 里 `slice_rows` 的依据。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `density_network` 的输出维度（`n_output_dims`）从 16 改成 32，颜色网络的输入维度会变成多少？
**答案**：`rgb_network` 输入 = `dir_encoding 输出宽 + density_network 输出宽`，再按 `rgb_alignment`（默认 16）向上取整（`next_multiple`）。密度输出翻倍会让颜色输入的前半段变宽。

**练习 2**：为什么密度头只吃位置、不吃方向？
**答案**：因为体密度 \(\sigma\) 是位置的固有属性（哪里有物质），与观察方向无关；把方向塞进密度头反而会干扰学习，并增加无谓计算。

---

### 4.2 四组件详解与配置对照

#### 4.2.1 概念说明

`NerfNetwork` 私有持有四个组件，与 `base.json` 的四块配置一一对应：

| 组件（成员） | 配置块 | 作用 | 默认值（base.json） |
|------|------|------|------|
| `m_pos_encoding` | `encoding` | 位置编码（坐标→特征） | `HashGrid`，`n_levels=8`，`n_features_per_level=4`，`log2_hashmap_size=19`，`base_resolution=16` |
| `m_density_network` | `network` | 密度 MLP（位置特征→密度特征） | `FullyFusedMLP`，`n_neurons=64`，`n_hidden_layers=1`，输出默认 16 维 |
| `m_dir_encoding` | `dir_encoding` | 方向编码（方向→特征） | `Composite`：`SphericalHarmonics(degree=4)` + `Identity` |
| `m_rgb_network` | `rgb_network` | 颜色 MLP（[密度特征‖方向特征]→RGB） | `FullyFusedMLP`，`n_neurons=64`，`n_hidden_layers=2`，输出 3 维 |

注意两个易混淆点：

- `base.json` 里那块叫 `"network"` 的配置，**其实是密度网络的配置**（不是整个网络）。`reset_network` 把它作为 `density_network` 参数传给 `NerfNetwork` 构造函数。
- 颜色相关的配置名是 `rgb_network`（MLP）和 `dir_encoding`（方向编码）。方向编码默认用**球谐函数**（`SphericalHarmonics`），不是哈希网格——因为方向是低频信号，球谐更合适、参数更省。

#### 4.2.2 核心流程

`NerfNetwork` 构造函数（`reset_network` 调用它时）按以下顺序建组件：

1. 先确定**对齐**：位置编码的对齐由密度网络 `otype` 决定（`FullyFusedMLP`/`MegakernelMLP` → 16，否则 8）；颜色网络对齐由 `minimum_alignment(rgb_network)` 决定。
2. 建两个编码 `m_pos_encoding`、`m_dir_encoding`。
3. 建密度网络 `m_density_network`：输入宽 = 位置编码的 `padded_output_width`；若配置没写 `n_output_dims` 则默认 **16**。
4. 算 `m_rgb_network_input_width = next_multiple(方向编码输出宽 + 密度网络输出宽, 颜色对齐)`。
5. 建颜色网络 `m_rgb_network`：输入 = 上一步算出的宽，输出固定 **3**。
6. 额外用第 1、3 步的积木拼一个 `m_density_model = NetworkWithInputEncoding(pos_encoding, density_network)`——供 4.4 节的「只算密度」捷径使用。

#### 4.2.3 源码精读

配置四块原文：

[configs/nerf/base.json:L23-L56](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L23-L56) — `encoding`（位置 HashGrid）、`network`（密度 `FullyFusedMLP`，64 神经元 1 隐层）、`dir_encoding`（球谐 degree 4 + Identity）、`rgb_network`（颜色 `FullyFusedMLP`，64 神经元 2 隐层）。注意 `network` 块**没写** `n_output_dims`，故密度输出走默认值 16。

构造函数逐组件建立（核心 6 步的代码落点）：

[include/neural-graphics-primitives/nerf_network.h:L81-L101](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L81-L101) — 构造函数：第 82 行按密度网络 `otype` 选位置编码对齐（16/8）；第 86–91 行给密度网络补 `n_input_dims`/`n_output_dims`(默认 16)；第 93 行算颜色网络输入宽；第 95–98 行建颜色网络（输出 3）；第 100 行额外拼 `m_density_model`。

`reset_network` 真正实例化 `NerfNetwork` 并接到顶层成员的地方：

[src/testbed.cu:L4283-L4299](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4283-L4299) — 为每块 GPU `make_shared<NerfNetwork>(dims.n_pos, n_dir_dims=3, n_extra_dims, dims.n_pos+1, encoding, dir_encoding, network, rgb_network)`；随后 `m_network = m_nerf_network = primary_device().nerf_network()`，`m_encoding = m_nerf_network->pos_encoding()`。注意第 4 个参数 `dims.n_pos + 1`（即方向偏移）——那个 `+1` 来自 `NerfCoordinate` 结构体里夹在位置与方向之间的 `dt` 成员，代码注释自嘲为 `HACKY`。

构造完成后，`reset_network` 还会打印两行日志，把实际算出的维度告诉你（启动 instant-ngp 时能看到）：

[src/testbed.cu:L4301-L4310](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4301-L4310) — 打印 `Density model: 3 --[HashGrid]--> <编码宽> --[FullyFusedMLP(...)]--> 1` 与 `Color model: 3 --[Composite]--> <方向编码宽>+16 --[FullyFusedMLP(...)]--> 3`，把双头结构用文字串了出来。

#### 4.2.4 代码实践

1. **目标**：验证「密度输出 16 维、颜色输入 = 方向编码宽 + 16」。
2. **步骤**：在已编译的 instant-ngp 中加载任意 NeRF 场景（如 `./instant-ngp data/nerf/fox`），观察启动日志中 `Density model` 与 `Color model` 两行。
3. **观察**：`Color model` 行里有一个 `+16`，那就是密度网络输出宽被拼进了颜色网络输入。
4. **预期结果**：日志显示密度网络输出 1（单值密度），颜色网络输出 3（RGB）。若看不到（无 GPU 环境），则记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么方向编码用球谐函数而不是哈希网格？
**答案**：方向只有 3 维且是低频信号，球谐函数是它的「天然基底」，参数极少（degree=4 仅 16 系数）；哈希网格是为高频、大空间设计的，用在方向上既浪费又难训。

**练习 2**：`base.json` 的 `network` 块没写 `n_output_dims`，密度网络输出宽是多少？代码在哪兜底？
**答案**：16 维。兜底在构造函数第 88–90 行（`if (!density_network.contains("n_output_dims")) ... = 16`）。

---

### 4.3 forward_impl：密度特征的零拷贝复用与「第 4 维写入」

#### 4.3.1 概念说明

这是本讲最精妙的一段代码。它要解决一个问题：**密度网络的输出特征，既要用来算最终密度，又要作为颜色网络的输入，怎么避免显存拷贝？**

答案是 **`slice_rows`（行切片视图）**：让「密度网络的输出缓冲区」和「颜色网络的输入缓冲区」**指向同一块显存**——密度网络直接把结果写进颜色网络输入的「前若干行」，方向编码再写「后若干行」，颜色网络最后读整块。全程零拷贝。

之后再有一个叫 `extract_density` 的小 kernel，把密度特征的值**抄一份**写进最终输出张量的**第 4 个分量**（0 基下标为 3），于是输出变成 `[R, G, B, density]` 共 4 维（即 `output_width() == 4`）。

#### 4.3.2 核心流程

`forward_impl` 的步骤（带训练用，会保存中间结果到 `ForwardContext`）：

1. 申请 `density_network_input`（位置特征缓冲）和 `rgb_network_input`（颜色网络输入缓冲，宽度 = `m_rgb_network_input_width`）。
2. `pos_encoding.forward(位置)` → 写入 `density_network_input`。
3. **关键复用**：令 `density_network_output = rgb_network_input.slice_rows(0, 密度网络输出宽)`——这是同一块显存的「前段视图」。
4. `density_network.forward(density_network_input)` → 写入 `density_network_output`（也就是写进了 `rgb_network_input` 的前段）。
5. 令 `dir_out = rgb_network_input.slice_rows(密度网络输出宽, 方向编码宽)`——同一块显存的「后段视图」。
6. `dir_encoding.forward(方向)` → 写入 `dir_out`（写进 `rgb_network_input` 后段）。**至此 `rgb_network_input` 前 = 密度特征、后 = 方向特征，已拼好。**
7. `rgb_network.forward(rgb_network_input)` → 写入最终 `output` 的前 3 维（RGB）。
8. `extract_density` kernel：把 `density_network_output` 的值抄进 `output` 第 4 维（下标 3）。

反向 `backward_impl` 则对称地把梯度沿同一条链传回去，并用 `extract_rgb`/`add_density_gradient` 两个 kernel 处理 RGB 梯度与「来自输出第 4 维的密度梯度」。

#### 4.3.3 源码精读

`forward_impl` 主体（8 步的代码落点）：

[include/neural-graphics-primitives/nerf_network.h:L145-L187](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L145-L187) — 完整前向。第 154–160 行位置编码；第 162–163 行**密度特征零拷贝复用**（`density_network_output = rgb_network_input.slice_rows(0, ...)`）；第 165–172 行方向编码（`dir_out = rgb_network_input.slice_rows(...)`）；第 178 行颜色网络；第 180–184 行 `extract_density` 写第 4 维。

最关键的复用点：

[include/neural-graphics-primitives/nerf_network.h:L162-L163](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L162-L163) — `density_network_output` 是 `rgb_network_input` 的前段切片视图，密度网络写它，等于直接写进颜色网络的输入缓冲。

把密度写进输出第 4 维的 kernel：

[include/neural-graphics-primitives/nerf_network.h:L32-L43](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L32-L43) — `extract_density`：每个线程把 `density[i]` 写到 `rgbd[i*rgbd_stride]`，配合调用处 `output->data()+3`，即写到每行的第 4 个分量（下标 3）。

调用 `extract_density` 的地方：

[include/neural-graphics-primitives/nerf_network.h:L180-L184](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L180-L184) — 前向结尾调用 `extract_density`，把密度抄进输出第 4 维，使最终输出 `[R,G,B,density]`（4 维）。

输出宽度声明（佐证「4 维 RGBD」）：

[include/neural-graphics-primitives/nerf_network.h:L400-L402](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L400-L402) — `output_width()` 固定返回 4（RGBD）；`padded_output_width()` 取 `max(颜色网络 padded 输出, 4)`（u3-l3 讲过 padding）。

反向路径里处理「输出第 4 维密度梯度」的 kernel（对称地「加回去」）：

[include/neural-graphics-primitives/nerf_network.h:L62-L74](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L62-L74) — `add_density_gradient`：反向时把来自输出第 4 维（`rgbd[i*rgbd_stride+3]`）的梯度**累加**到密度特征的梯度上（因为密度特征同时影响了「密度输出」和「颜色网络输入」两条路径）。

#### 4.3.4 代码实践（本讲主实践）

1. **目标**：在 `forward_impl` 中追踪一个输入张量，标出它依次经过的四个组件，并说清密度如何写入输出第 4 维。
2. **操作步骤**：
   - 打开 `nerf_network.h` 第 145–187 行。
   - 准备一支笔，按下面 5 个锚点标注数据流向：
     1. `input.slice_rows(0, m_pos_encoding->input_width())`（第 156 行）→ 取出**位置**（前 3 维）。
     2. `m_pos_encoding->forward(...)`（第 154 行）→ 位置特征。
     3. `m_density_network->forward(...)`（第 163 行）→ 密度特征，写入 `density_network_output`（即 `rgb_network_input` 前段）。
     4. `input.slice_rows(m_dir_offset, ...)`（第 168 行）→ 取出**方向**（注意从 `m_dir_offset = dims.n_pos+1 = 4` 开始，跳过了位置和那个 `dt`）。
     5. `m_rgb_network->forward(...)`（第 178 行）→ RGB，写入 `output` 前 3 维。
   - 最后看第 181–183 行的 `extract_density` 调用，注意 `output->data()+3`。
3. **需要观察的现象**：`density_network` 和 `dir_encoding` 的输出**共享** `rgb_network_input` 这同一块显存（一个写前段、一个写后段），没有显式 `cudaMemcpy`。
4. **预期结果**：你能画出「`rgb_network_input` 的前 16 列是密度特征、后若干列是方向特征、颜色网络一次读完整块」的内存布局图，并解释 `extract_density` 用 `+3` 把密度放到第 4 维（下标 3）。
5. 如果无法在本地跑 GPU 验证布局，相关结论标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：反向时为什么 `add_density_gradient` 是「累加（`+=`）」而不是「赋值」？
**答案**：因为同一个密度特征既流向了「密度输出」（第 4 维），又流向了「颜色网络输入」。密度特征的梯度 = 来自这两条路径的梯度之和，所以要把第 4 维回传的梯度**加到**颜色网络回传的梯度上。

**练习 2**：`m_dir_offset` 为什么是 `dims.n_pos + 1` 而不是 `dims.n_pos`？
**答案**：输入张量按 `NerfCoordinate` 结构体紧凑排布为 `[位置(3), dt(1), 方向(3), ...]`，方向前面多了一个 `dt`，所以方向起始偏移要 `+1`。这个 `+1` 在 `testbed.cu:4287` 的注释里被标为 `HACKY`。

---

### 4.4 密度专用前向：density / density_forward

#### 4.4.1 概念说明

现在考虑一个新需求：**密度网格更新**（u4-l5 详讲）需要在大约 \(N = 2 \times \text{NERF\_GRID\_N\_CELLS}\)（约 26 万）个网格点上**只查询密度**，不需要颜色。如果走完整 `forward_impl`，会白白跑 `dir_encoding` + `rgb_network`，而这些点上「方向」根本没有意义（密度网格采样的是空间均匀/非均匀点，不带观察方向）。

于是 `NerfNetwork` 专门提供**只走 `[pos_encoding → density_network]`** 的捷径接口：

- `density(stream, input, output, ...)`：推理用的「只算密度」，内部委托给 `m_density_model`（就是构造时用第 1、3 步积木拼的那个 `NetworkWithInputEncoding`）。
- `density_forward(...)` / `density_backward(...)`：训练用的「只算密度」前向/反向，走 `ForwardContext` 那套保存中间结果的机制（供网格优化 `optimise_mesh_step` 等需要反向的场景）。

这两个接口的存在，是本仓库「为高频查询单独优化路径」思想的体现——和 u4-l5 的密度网格、u4-l4 的误差图采样一脉相承。

#### 4.4.2 核心流程

`density()` 的执行：

```
位置输入（列主序 CM）
   └─► m_density_model.inference_mixed_precision(...)   // = NetworkWithInputEncoding(pos_encoding + density_network)
          └─► 输出密度特征（padded_density_output_width 维）
```

注意它**完全不碰** `dir_encoding` 和 `rgb_network`，因此比 `forward_impl` 少跑两个组件。调用方拿到密度特征后，自己用 `network_to_density(..., density_activation)` 把它激活成最终体密度 \(\sigma\)（这部分在 `testbed_nerf.cu` 的各 fused kernel 里，见第 278、633、936 行等）。

#### 4.4.3 源码精读

构造时额外拼出 `m_density_model`：

[include/neural-graphics-primitives/nerf_network.h:L100-L100](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L100) — `m_density_model = NetworkWithInputEncoding(pos_encoding, density_network)`，复用了密度头的两个积木，专门给「只算密度」用。

`density()` 实现（推理捷径）：

[include/neural-graphics-primitives/nerf_network.h:L270-L280](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L270-L280) — 只做 `pos_encoding → density_network`（经 `m_density_model`），要求输入列主序（CM），并把自己的 `jit_fusion` 设置透传给 `m_density_model`。

`density_forward()` 实现（训练捷径，保存中间结果）：

[include/neural-graphics-primitives/nerf_network.h:L282-L309](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L282-L309) — 与 `forward_impl` 相比，只保留位置编码和密度网络两步，不申请 `rgb_network_input`、不跑方向编码和颜色网络。

密度网格更新调用 `density()` 的地方：

[src/testbed_nerf.cu:L2559-L2571](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2559-L2571) — 在网格点上批量采样密度（`batch_size = NERF_GRID_N_CELLS()*2`，分批避免超过 cutlass 索引范围），调用 `m_nerf_network->density(stream, ...)`。

网格优化流程调用 `density()` + `input_gradient()` 的地方：

[src/testbed_nerf.cu:L3429-L3432](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3429-L3432) — `optimise_mesh_step` 用 `density()` 拿密度、用 `input_gradient()` 拿「密度对位置的梯度」，把网格顶点推向等密度面（u6-l4 出网格的前置步骤）。

> 旁证：`m_nerf_network` 在顶层 `testbed.h` 中作为 NeRF 专用成员单独声明，NeRF 模式下与 `m_network` 指向同一对象（u2-l1 已述），正是因为只有 `NerfNetwork` 才暴露 `density()` 等专用接口。

#### 4.4.4 代码实践

1. **目标**：对比 `forward_impl` 与 `density_forward` 的组件开销。
2. **步骤**：
   - 数 `forward_impl`（145–187 行）里调用了几个 `.forward`：应为 4 个（`pos_encoding`、`density_network`、`dir_encoding`、`rgb_network`）。
   - 数 `density_forward`（282–309 行）里调用了几个 `.forward`：应为 2 个（`pos_encoding`、`density_network`）。
3. **观察**：两者还差一个 `extract_density` kernel——`density_forward` 不需要，因为它的输出本身就只有密度。
4. **预期结果**：你能说清「密度网格每轮更新省下了方向编码 + 颜色网络两次前向」，这是 instant-ngp 能秒级训练的细节之一。

#### 4.4.5 小练习与答案

**练习 1**：`density()` 为什么要求输入是列主序（CM）？
**答案**：因为它把输入直接交给 `m_density_model`（`NetworkWithInputEncoding`）做 `inference_mixed_precision`，该路径按列主序布局批数据以保证访存合并；非 CM 会直接抛 `runtime_error`。

**练习 2**：既然有了 `forward_impl`（也能算密度），为什么还要单独的 `density()`？
**答案**：密度网格/网格优化在数十万点上**只**需要密度，走 `forward_impl` 会多算方向编码与颜色网络（且这些点没有有意义的方向），浪费算力。`density()` 只跑密度头，开销约为完整前向的一半。

---

## 5. 综合实践

把本讲四节串起来，完成下面这张「`NerfNetwork` 全景图」任务：

1. **画图**：在纸上（或文本里）画出 `NerfNetwork` 的完整数据流，要求标注：
   - 输入张量的字段划分（`位置 | dt | 方向 | extra`）与方向偏移 `m_dir_offset`；
   - 四个组件 `pos_encoding / density_network / dir_encoding / rgb_network` 的位置；
   - `rgb_network_input` 缓冲区被「密度特征（前段）+ 方向特征（后段）」共享复用；
   - `extract_density` 把密度写进输出第 4 维。
2. **配对**：把图上每个组件对应到 `configs/nerf/base.json` 的一块配置，写出默认 `otype`。
3. **分流**：在图上另画一条「密度专用」支线，标出 `density()` 经过 `m_density_model` 只跑前两个组件，并指出它在 `testbed_nerf.cu` 的密度网格更新（2559–2571 行）和网格优化（3429–3432 行）被调用。
4. **反思**：用一句话回答——「为什么 NeRF 的网络要拆成双头、还要给密度单开一条前向捷径？」

**参考答案要点**：拆双头是因为密度只依赖位置、颜色还依赖方向，分开建模更准更省；单开密度捷径是因为密度网格/网格优化要在数十万点上高频查密度、且不需要颜色，专路能砍掉一半算力。两者共同服务于 instant-ngp「秒级训练」的目标。

---

## 6. 本讲小结

- `NerfNetwork` 是**双头**网络：密度头（`pos_encoding` + `density_network`）只吃位置、产出密度特征；颜色头（`dir_encoding` + `rgb_network`）吃「密度特征 ‖ 方向特征」、产出 RGB。
- 四个组件对应 `base.json` 的 `encoding / network / dir_encoding / rgb_network` 四块；注意 `network` 块其实是**密度网络**配置，密度输出默认 16 维。
- `forward_impl` 用 `slice_rows` 让密度网络输出与颜色网络输入**共享同一块显存**（密度特征在前段、方向特征在后段），实现零拷贝复用。
- 最终输出是 4 维 RGBD：RGB 由颜色网络给出，密度由 `extract_density` kernel 从密度特征抄进第 4 维（下标 3）。
- `density()` / `density_forward()` 是「只算密度」的捷径（经 `m_density_model`），供密度网格更新和网格优化高频调用，省下方向编码与颜色网络的开销。
- 反向 `backward_impl` 用 `add_density_gradient` 把「输出第 4 维的密度梯度」累加回密度特征梯度，对应密度特征同时影响密度输出与颜色网络两条路径。

## 7. 下一步学习建议

- **u4-l3（NeRF 光线步进与体渲染）**：本讲的 `forward_impl` / JIT 设备函数产出的是单点 `[R,G,B,density]`，下一讲讲这些点如何沿光线被采样、用 alpha 合成拼成像素，并解释 `network_to_density` 如何把密度特征激活成 \(\sigma\)。
- **u4-l5（密度网格）**：本讲多次提到的 `density()` 调用方，下一讲会完整讲清密度网格如何 EMA 更新、压成位域、用于空区域跳过。
- **u8-l2（JIT 融合）**：本讲引用的 `generate_device_function`（490–499 行）是运行时编译把四组件融合成单一内核的入口，专家层会详讲其拼接与缓存机制。
- 想自行实验：拷贝 `configs/nerf/base.json`，改 `network.n_neurons` 或 `dir_encoding.degree`，用 `./instant-ngp data/nerf/fox -c <你的配置>` 观察启动日志里 `Density model`/`Color model` 两行的维度变化（结合本讲 4.2.4）。
