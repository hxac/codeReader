# NerfNetwork 双头架构

## 1. 本讲目标

本讲是「NeRF 原语深入」的第二篇。上一篇（u4-l1）解决了「数据怎么进显存」——把 `transforms.json` + 图像加载成 `NerfDataset`。本讲顺着数据流往下走一步：**当一条采样点（位置 + 方向）送进神经网络后，它如何变成「颜色 + 密度」这四个数**。

读完本讲，你应当能够：

- 说清 NeRF 为何把网络拆成「密度头」和「颜色头」两段，而不是一个端到端大 MLP；
- 在 `nerf_network.h` 中认出 `pos_encoding / dir_encoding / density_network / rgb_network` 这四个核心组件，并指出它们与 `configs/nerf/base.json` 四块配置的对应关系；
- 跟踪 `forward_impl` 中一个输入张量依次流经四个组件的顺序，并能解释「密度特征如何被复用到颜色网络的输入」；
- 解释为什么 `NerfNetwork` 还要单独提供 `density()` / `density_forward()` 这条「只算密度」的捷径，以及它和密度网格、网格提取的关系。

本讲只聚焦网络结构本身，不展开渲染（u4-l3）、训练循环（u4-l4）、密度网格（u4-l5），它们各有专讲。

---

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是 NeRF 的「辐射场」。** NeRF（Neural Radiance Field，神经辐射场）把一个三维场景建模成一个函数 \(F\)：给它空间中任意一点的位置 \(\mathbf{x}\) 和观察方向 \(\mathbf{d}\)，它返回该点的**颜色** \(\mathbf{c}\)（RGB，3 维）和**体密度** \(\sigma\)（一个标量，表示这个点有多「不透明」）。体渲染方程会把沿光线的所有 \((\mathbf{c}, \sigma)\) 累积成最终像素颜色。所以网络的输出是一个 4 维向量：\([\,R, G, B, \sigma\,]\)。

**为什么密度和颜色要分两个头。** 原始 NeRF 论文（Mildenhall et al., 2020）发现一个关键经验：**密度主要由位置决定**（一个点是不是物体表面，只和它在哪有关），**颜色则还要看方向**（同一个点从不同角度看、或在不同光照下，颜色不同——这是「镜面反射/视角相关性」）。因此网络被设计成两段：

1. 先用一个**密度 MLP** 只吃位置 \(\mathbf{x}\)，得到「密度」以及一组「密度特征」；
2. 再把方向 \(\mathbf{d}\) 编码后，**拼接到密度特征后面**，喂给**颜色 MLP**，得到 RGB。

这样位置信息走完整的两段、方向信息只影响后半段——既符合物理直觉，又省算力（颜色 MLP 不必重新处理位置）。

**什么是「密度特征复用」。** 密度 MLP 输出的不是一个标量，而是一整组特征向量（默认 16 维）。其中**第 0 维**被解释为体密度 \(\sigma\)（渲染时还要过一次激活），而**全部 16 维**都被当作「这个点的几何/材质摘要」送给颜色 MLP 当条件。换句话说，密度头一身二职：既输出标量密度，又为颜色头提供位置的高层特征。

> 提示：本仓库（instant-ngp）是**应用层**，`NerfNetwork` 只是把 tiny-cuda-nn 的积木（`Encoding`、`Network`、`NetworkWithInputEncoding`）拼成这个双头结构；单个 MLP 与编码的内核实现在外部依赖 tiny-cuda-nn 中（见 u3-l1、u3-l2、u3-l3）。

---

## 3. 本讲源码地图

本讲只涉及三个核心文件，逻辑都集中在头文件里，阅读量不大：

| 文件 | 作用 |
| --- | --- |
| `include/neural-graphics-primitives/nerf_network.h` | `NerfNetwork` 类的全部声明与实现（模板内联）。本讲的主角：双头结构、四个组件、前向/反向、密度专用前向都在这里。 |
| `include/neural-graphics-primitives/testbed.h` | 仅用于确认 `m_nerf_network` 成员的类型（`std::shared_ptr<NerfNetwork<network_precision_t>>`），以及它与 `m_network` 的关系（见 u2-l1、u3-l3）。 |
| `configs/nerf/base.json` | NeRF 默认网络配置：四组件各自的参数（`encoding`/`network`/`dir_encoding`/`rgb_network`）。 |

另外会少量引用 `src/testbed.cu`（`reset_network` 里如何构造 `NerfNetwork`）、`src/testbed_nerf.cu`（`network_dims_nerf`、密度网格如何调用 `density()`）、`include/neural-graphics-primitives/nerf_device.cuh`（`NerfCoordinate` 输入布局）作为佐证，但不在这些文件里展开。

---

## 4. 核心概念与源码讲解

### 4.1 双头 MLP：为什么 NeRF 要把密度和颜色分开

#### 4.1.1 概念说明

`NerfNetwork` 是 instant-ngp 对「位置→密度、(密度特征+方向)→颜色」这一两段式结构的工程化封装。文件开头的注释一句话点题：

> A network that first processes 3D position to density and subsequently direction to color.
> （先处理 3D 位置得到密度，随后处理方向得到颜色。）

它对外仍是一个 `Network<float, T>`（继承自 tiny-cuda-nn 的网络基类），输入是「一列采样点」、输出是「一列 4 维 \([R,G,B,\sigma]\)」——从调用方（`Trainer`、渲染器）看，它和单头网络没区别，**双头是内部实现细节**。这种「外壳统一、内部分头」的设计让 `Trainer` 只需持有一个 `m_network` 就能端到端训练整个 NeRF（见 u3-l3）。

#### 4.1.2 核心流程

一条采样点（位置 \(\mathbf{x}\)、方向 \(\mathbf{d}\)）流过双头结构的过程，可以用下面的数据流图描述：

```
输入张量 (一列采样点: [x,y,z, dt, dx,dy,dz, extra...])
   │
   ├──(取位置行)──► pos_encoding ──► 位置特征 (默认 8×4=32 维)
   │                                        │
   │                                        ▼
   │                                 density_network ──► 密度特征 (默认 16 维)
   │                                        │       │
   │                                        │       └──(第 0 维)──► σ (体密度)
   │                                        │
   ├──(取方向行)──► dir_encoding ──► 方向特征 (默认 SH degree4=25 维)
   │                                        │
   └──────────────────────────────────► [密度特征 ‖ 方向特征] (拼接)
                                                  │
                                                  ▼
                                           rgb_network ──► [R, G, B]

最终输出: [R, G, B, σ]   (σ 由 extract_density 核函数补写到第 4 维)
```

三个要点：

1. **位置走两段、方向走半段**：位置特征经过密度 MLP 得到密度特征；方向只编码后拼到密度特征后面，不再单独过密度 MLP。
2. **密度特征一身二职**：它既向渲染提供标量 \(\sigma\)（取第 0 维），又整组送给颜色 MLP 当条件。
3. **输出拼装**：RGB 由颜色 MLP 产出，\(\sigma\) 由一个轻量 CUDA 核 `extract_density` 从密度特征里抠出来补到第 4 维——两者来自不同的头，最后在输出缓冲里拼成 4 维。

体渲染最终用到的 \(\sigma\) 还要经过一次激活（`density_activation`，见 u4-l3 渲染与 u4-l5 密度网格），但那是渲染器的事；网络本身输出的就是密度特征的原始第 0 维。

#### 4.1.3 源码精读

**类的整体形态。** `NerfNetwork` 是一个模板类，继承 tiny-cuda-nn 的 `Network<float, T>`，其中 `T` 是权重精度（混合精度下为半精度）：

- 类声明与构造函数：[include/neural-graphics-primitives/nerf_network.h:77-101](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L77-L101) —— 构造函数把「四组件 + 维度信息」一次性传进来，对应 `configs/nerf/base.json` 里的四块。

**输出宽度是 4。** 几个关键尺寸方法直接告诉我们输出形状：

- `output_width()` 返回 `4`（RGB + 密度）：[nerf_network.h:400-402](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L400-L402)
- `padded_output_width()` 取 `max(rgb_network 宽度, 4)`，保证至少能放下 RGBA：[nerf_network.h:392-394](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L392-L394)

这两个方法说明：**对外输出固定是 4 维 \([R,G,B,\sigma]\)**，这是体渲染器（`render_nerf`）和训练损失所依赖的契约。

**密度头产出 16 维特征、只用第 0 维当密度。** 构造函数里，如果配置没写 `n_output_dims`，密度网络的输出维度被默认设成 16：

```cpp
if (!density_network.contains("n_output_dims")) {
    local_density_network_config["n_output_dims"] = 16;
}
m_density_network.reset(create_network<T>(local_density_network_config));
```

见 [nerf_network.h:86-91](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L86-L91)。`configs/nerf/base.json` 的 `network` 块只写了 `n_neurons=64, n_hidden_layers=1`，没写 `n_output_dims`，所以密度头实际输出 16 维——其中第 0 维是 \(\sigma\)，全部 16 维送给颜色头。

把密度补写到第 4 维的轻量核函数 `extract_density` 只取密度的第 0 个分量：

```cpp
// 每个 thread 处理一个采样点：把密度特征的第 0 维写到输出对应槽位
rgbd[i * rgbd_stride] = density[i * density_stride];
```

见 [nerf_network.h:31-43](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L31-L43)。注意 `density_stride`/`rgbd_stride` 会随矩阵是行主序（AoS）还是列主序（RM）而不同，但语义始终是「取该采样点密度的第 0 维，写到该采样点输出的第 4 个槽位」。

**参数空间把四个组件摊平。** `n_params()` 和 `set_params_impl()` 把四个组件的参数首尾相接放进同一块连续显存，让 `Trainer` 用一套优化器管全部参数：

- 参数总量：`pos_encoding + density_network + dir_encoding + rgb_network`：[nerf_network.h:388-390](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L388-L390)
- 参数指针分配（偏移顺序是 density_model→density→rgb→pos_enc→dir_enc）：[nerf_network.h:357-372](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L357-L372)

> 这也是为什么 u3-l3 强调「`Trainer` 只持有一个 `m_network`」就能同时训练哈希表与 MLP 权重——`NerfNetwork` 把多组件折叠成一个参数空间。

#### 4.1.4 代码实践

**实践目标**：确认双头结构在真实代码里的「两段」边界。

**操作步骤**：

1. 打开 [include/neural-graphics-primitives/nerf_network.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h)，定位到构造函数（L77-L101）。
2. 找到 `m_density_network.reset(...)` 这一行（L91）——这是「密度头」被造出来的地方。
3. 往下找到 `m_rgb_network.reset(...)`（L98）——这是「颜色头」被造出来的地方。
4. 在两行之间，找到 `m_rgb_network_input_width = next_multiple(...)`（L93）：它把「密度头输出宽度」和「方向编码输出宽度」加起来，作为颜色头的输入宽度。

**需要观察的现象**：颜色头的输入宽度（`m_rgb_network_input_width`）由两部分相加而成——`m_density_network->padded_output_width()`（密度特征）与 `m_dir_encoding->padded_output_width()`（方向特征）。这正是「密度特征被复用到颜色网络输入」的直接证据。

**预期结果**：你会清楚地看到，颜色 MLP 的输入 = 密度特征 ‖ 方向特征，二者在 `rgb_network_input` 这个缓冲里前后拼接（见下一节 4.2.3 的 `slice_rows`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把密度网络的 `n_output_dims` 从默认 16 改成 1，会发生什么？颜色头还能正常工作吗？

**参考答案**：密度头只输出 1 维（即 \(\sigma\) 本身），`m_density_network->padded_output_width()` 变小，颜色头输入宽度随之变窄。颜色头失去了「位置的高层特征摘要」，只剩方向特征作条件——视角相关性还在，但颜色 MLP 难以仅凭方向区分不同位置的表面颜色，拟合质量通常会下降。这正是原始 NeRF 让密度头输出多维「特征向量」的原因。注意实际还要受 `FullyFusedMLP` 的 16 对齐约束（见 u3-l3），改这个值需同步考虑对齐。

**练习 2**：`output_width()` 为什么返回 4 而不是 3？

**参考答案**：因为体渲染不仅需要 RGB，还需要体密度 \(\sigma\)。网络把 \([R,G,B,\sigma]\) 一起输出（\(\sigma\) 由 `extract_density` 补写到第 4 维），渲染器据此做 alpha 合成。3 维只能给颜色，给不了不透明度。

---

### 4.2 四个组件与输入张量布局

#### 4.2.1 概念说明

双头结构由**四个可独立配置的组件**拼成，它们与 `configs/nerf/base.json` 的四块一一对应：

| 组件（C++ 成员） | 配置块 | 默认配置 | 职责 |
| --- | --- | --- | --- |
| `m_pos_encoding` | `encoding` | HashGrid, L=8, F=4, T=2¹⁹ | 把 3D 位置编码成高维特征（多分辨率哈希编码，核心创新，见 u3-l2） |
| `m_density_network` | `network` | FullyFusedMLP, 64×1 | 位置特征 → 16 维密度特征（第 0 维即 \(\sigma\)） |
| `m_dir_encoding` | `dir_encoding` | SH(degree 4) + Identity | 把观察方向编码成球谐特征 |
| `m_rgb_network` | `rgb_network` | FullyFusedMLP, 64×2 | (密度特征 ‖ 方向特征) → RGB |

注意 `base.json` 里 `encoding`/`network` 服务密度头，`dir_encoding`/`rgb_network` 服务颜色头——**两套编码 + 两套 MLP**，正好对应双头。对应的配置行：`encoding` 块 [base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L23-L29)、`network` 块 [base.json:30-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L30-L36)、`dir_encoding` 块 [base.json:37-49](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L37-L49)、`rgb_network` 块 [base.json:50-56](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L50-L56)。

#### 4.2.2 核心流程：输入张量怎么布局

四个组件共享同一个输入矩阵。理解 `forward_impl` 的前提是搞清楚「一列采样点」在显存里长什么样。采样点用 `NerfCoordinate` 结构体打包：

```cpp
struct NerfCoordinate {
    NerfPosition pos;   // vec3 p  → 占 3 个 float
    float dt;           // 沿光线的步距 → 1 个 float
    NerfDirection dir;  // vec3 d  → 3 个 float
    // 后面紧跟 extra_dims（每图潜码，见 u8-l3）
};
```

见 [include/neural-graphics-primitives/nerf_device.cuh:176-184](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L176-L184) 与 `NerfPosition`（[nerf_device.cuh:157-169](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_device.cuh#L157-L169)）。

所以每个采样点在内存里是一串浮点数：`[px, py, pz, dt, dx, dy, dz, extra...]`。把一整批采样点按列排成矩阵后，**行轴就是特征维度**：

- 第 0–2 行：位置 \((x,y,z)\)；
- 第 3 行：`dt`（步距，被跳过，见下方 `dir_offset`）；
- 第 4–6 行：方向 \((d_x,d_y,d_z)\)；
- 第 7 行起：extra dims。

于是位置头读「前 3 行」、方向头读「从 `dir_offset` 开始的几行」，两者从同一块输入里各取所需。`dir_offset` 就是方向在行轴上的起始位置，构造时由 `reset_network` 传入：

```cpp
dims.n_pos + 1, // The offset of 1 comes from the dt member variable of NerfCoordinate. HACKY
```

见 [src/testbed.cu:4287](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4287)。`n_pos=3`，加 1 跳过 `dt`，所以方向从第 4 行开始——这就是注释里说的「HACKY」：靠结构体里恰好有个 `dt` 字段来对齐偏移。

`input_width()` 据此算出总输入宽度：`dir_offset(4) + n_dir_dims(3) + n_extra_dims`，见 [nerf_network.h:396-398](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L396-L398)。而 `network_dims_nerf()` 给出 `n_pos = sizeof(NerfPosition)/sizeof(float) = 3`、`n_output = 4`：

见 [src/testbed_nerf.cu:55-61](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L55-L61)。

#### 4.2.3 源码精读：`forward_impl` 的四步流转

`forward_impl` 是训练时真正的「前向」入口（它比 `inference_mixed_precision_impl` 多保留了反向所需的中间激活，整体范围 [nerf_network.h:145-187](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L145-L187)）。下面按四个组件的调用顺序逐一对应源码，每一步都标出「从输入的哪几行读、写到哪个缓冲」。

**第 0 步：分配两个工作缓冲。**

```cpp
forward->density_network_input = GPUMatrixDynamic<T>{m_pos_encoding->padded_output_width(), batch_size, ...};
forward->rgb_network_input     = GPUMatrixDynamic<T>{m_rgb_network_input_width, batch_size, ...};
```

见 [nerf_network.h:151-152](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L151-L152)。注意 `rgb_network_input` 这块缓冲会被「密度特征」和「方向特征」**前后拼接共享**——这是复用的关键。

**第 1 步：位置编码（只读输入的位置行）。**

```cpp
forward->pos_encoding_ctx = m_pos_encoding->forward(
    stream,
    input.slice_rows(0, m_pos_encoding->input_width()),  // 读 [0,3) 行：位置
    &forward->density_network_input, ...);
```

见 [nerf_network.h:154-160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L154-L160)。`slice_rows(0, 3)` 正是取位置三行。

**第 2 步：密度 MLP（产出密度特征，写到 rgb 缓冲的前段）。**

```cpp
forward->density_network_output = forward->rgb_network_input.slice_rows(0, m_density_network->padded_output_width());
forward->density_network_ctx = m_density_network->forward(stream, forward->density_network_input, &forward->density_network_output, ...);
```

见 [nerf_network.h:162-163](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L162-L163)。**重点**：密度特征的输出直接落到 `rgb_network_input` 的前 16 行——也就是说，它已经「就位」等着和方向特征拼接了，无需额外拷贝。

**第 3 步：方向编码（只读输入的方向行，写到 rgb 缓冲的后段）。**

```cpp
auto dir_out = forward->rgb_network_input.slice_rows(m_density_network->padded_output_width(), m_dir_encoding->padded_output_width());
forward->dir_encoding_ctx = m_dir_encoding->forward(
    stream,
    input.slice_rows(m_dir_offset, m_dir_encoding->input_width()),  // 读 [4,7) 行：方向
    &dir_out, ...);
```

见 [nerf_network.h:165-172](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L165-L172)。方向特征落到 `rgb_network_input` 紧跟在密度特征之后的行段。至此 `rgb_network_input` = [密度特征(16) ‖ 方向特征(SH)]，拼接完成。

**第 4 步：颜色 MLP（吃拼接后的特征，吐 RGB）+ extract_density 补 σ。**

```cpp
forward->rgb_network_ctx = m_rgb_network->forward(stream, forward->rgb_network_input, output ? &forward->rgb_network_output : nullptr, ...);

if (output) {
    linear_kernel(extract_density<T>, 0, stream, batch_size,
        ..., forward->density_network_output.data(), output->data()+3);
}
```

见 [nerf_network.h:178-184](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L178-L184)。RGB 由 `m_rgb_network` 写入输出的前 3 维；随后 `extract_density` 把密度特征的第 0 维拷到 `output->data()+3`（即第 4 维）。最终输出每列为 \([R,G,B,\sigma]\)。

**反向传播**（`backward_impl`）严格按相反顺序回传梯度：先颜色 MLP、再方向编码、把密度的损失梯度累加回密度头、最后密度 MLP 与位置编码。其中 `add_density_gradient` 核函数负责把「来自第 4 维 \(\sigma\) 的梯度」叠加到密度特征的梯度上，见 [nerf_network.h:62-74](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L62-L74) 与 `backward_impl` 主体 [nerf_network.h:189-268](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L189-L268)。

**JIT 设备函数**把同样的四步融合进一个端到端 CUDA 设备函数（省去中间缓冲读写，见 u8-l2）。它返回的写法直观体现了「RGB 来自颜色头、σ 来自密度特征第 0 维」：

```cpp
return {{rgb_mlp_out[0], rgb_mlp_out[1], rgb_mlp_out[2], rgb_mlp_in[0]}};
//         └────── 颜色头 RGB ──────┘   └ 密度特征第0维=σ ┘
```

见 [nerf_network.h:476-520](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L476-L520)（`generate_device_function`，返回语句在 L499）。

#### 4.2.4 代码实践

**实践目标**：亲手在 `forward_impl` 里追踪一个输入张量，确认它经过四个组件的顺序，以及密度被写到第 4 维的事实。

**操作步骤**：

1. 打开 [include/neural-graphics-primitives/nerf_network.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h)，定位 `forward_impl`（L145-L187）。
2. 在源码上用笔/注释标出四个调用点：
   - ① `m_pos_encoding->forward(...)`（L154，输入 `slice_rows(0, ...)` = 位置）；
   - ② `m_density_network->forward(...)`（L163，输出落到 `rgb_network_input` 前段）；
   - ③ `m_dir_encoding->forward(...)`（L166，输入 `slice_rows(m_dir_offset, ...)` = 方向，输出落到 `rgb_network_input` 后段）；
   - ④ `m_rgb_network->forward(...)`（L178，吃整个 `rgb_network_input`）。
3. 找到末尾的 `linear_kernel(extract_density<T>, ...)`（L181-L183），确认它的目标地址是 `output->data()+3`。

**需要观察的现象**：①②③④ 的顺序与 4.2.3 描述完全一致；`extract_density` 的写入偏移 `+3` 把密度放进输出的第 4 个槽位（下标 3）。

**预期结果**：你能用一句话描述完整链路——「输入的位置行经 `pos_encoding`→`density_network` 得到密度特征（前 16 行）+ 方向行经 `dir_encoding` 得到方向特征（后段）拼接成 `rgb_network_input` → `rgb_network` 输出 RGB；密度的第 0 维由 `extract_density` 补写到输出下标 3」。

**待本地验证**：若你想亲眼看到「第 0 维即密度」，可在 `density()` 路径下（见 4.3）用 pyngp 对一个已训练 fox 快照在某点采样并打印密度特征——但单点 hook 需要 C++ 改动，属进阶操作，此处标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `forward_impl` 里方向编码用 `input.slice_rows(m_dir_offset, ...)` 而不是 `slice_rows(3, ...)` 写死 3？

**参考答案**：因为方向的起始行由 `m_dir_offset` 决定，而 `m_dir_offset = n_pos + 1`（构造时传入）。`n_pos` 来自 `sizeof(NerfPosition)/sizeof(float)`，在开启 `TRIPLANAR_COMPATIBLE_POSITIONS` 宏时 `NerfPosition` 会多一个 `float x` 字段（见 nerf_device.cuh:155-168），`n_pos` 变成 4，`dir_offset` 随之变为 5。用变量而非写死，保证位置维度变化时方向偏移自动跟随。

**练习 2**：`rgb_network_input` 这块缓冲被「密度特征」和「方向特征」共用拼接。这样做相比「各开一块再 copy 拼接」有什么好处？

**参考答案**：避免一次显存到显存的拷贝。密度 MLP 的输出直接写到 `rgb_network_input` 的前段（`slice_rows(0, ...)`），方向编码直接写到后段（`slice_rows(offset, ...)`），两段天然连续，颜色 MLP 立即可读。在 GPU 上省一次 kernel 启动和数据搬运，对每帧数百万采样点的 NeRF 是可观的优化。

---

### 4.3 密度专用前向：为密度网格服务

#### 4.3.1 概念说明

`NerfNetwork` 还提供了一组**只算密度、不算颜色**的方法：`density()` / `density_forward()` / `density_backward()`。它们的存在源于一个现实需求：

很多子系统**只需要密度 \(\sigma\)、根本不需要颜色**，例如：

- **密度网格**（u4-l5）：在成千上万个 3D 格子点上采样密度，构建一个「哪里有东西」的位图，用于渲染时跳过空区域。这里完全不需要 RGB。
- **Marching Cubes 网格提取**（u6-l4）：在 3D 网格上采样密度场做等值面提取，要的也是密度。
- **相机位姿优化**（u8-l3）：需要密度对位置的梯度来调整相机。

如果这些场景都走完整的双头前向，会白白算一遍方向编码 + 颜色 MLP——纯属浪费。于是 `NerfNetwork` 单独开了「密度专用前向」，只跑 `pos_encoding → density_network` 两步，便宜得多。

#### 4.3.2 核心流程

密度专用前向复用了一个叫 `m_density_model` 的「子模型」——它把 `m_pos_encoding` 和 `m_density_network` 用 tiny-cuda-nn 的 `NetworkWithInputEncoding` 再打包一遍：

```cpp
m_density_model = std::make_shared<NetworkWithInputEncoding<T>>(m_pos_encoding, m_density_network);
```

见 [nerf_network.h:100](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L100)（构造函数末尾）。

关键点：`m_density_model` **不持有独立的权重**。它通过 `set_params_impl()` 拿到与完整 `NerfNetwork` **同一块参数显存的指针**（[nerf_network.h:357-358](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L357-L358) 先把参数交给 `m_density_model`，再依次分给 density/rgb 等组件）。所以「密度专用前向」查询的是**完全相同的、已训练的**位置编码与密度头权重，只是跳过了颜色头——结果与完整前向里的 \(\sigma\) 完全一致，只是省了颜色那段的计算。

调用链：密度网格采样 → `m_nerf_network->density(stream, positions, density_matrix, ...)`，见 [src/testbed_nerf.cu:2570](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L2570)（密度网格更新）与 [src/testbed_nerf.cu:3430](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L3430)（网格提取）。

#### 4.3.3 源码精读

`density()` 方法极简：校验列主序输入后，把 JIT 融合设置同步给 `m_density_model`，然后直接调它的推理接口，输入只取位置行：

```cpp
void density(cudaStream_t stream, const GPUMatrixDynamic<float>& input, GPUMatrixDynamic<T>& output, bool use_inference_params = true) {
    if (input.layout() != CM) { throw ...; }  // 必须列主序
    uint32_t batch_size = output.n();
    GPUMatrixDynamic<T> density_network_input{m_pos_encoding->padded_output_width(), batch_size, stream, m_pos_encoding->preferred_output_layout()};

    m_density_model->set_jit_fusion(this->jit_fusion());
    m_density_model->inference_mixed_precision(stream, input.slice_rows(0, m_pos_encoding->input_width()), output, use_inference_params);
}
```

见 [nerf_network.h:270-280](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L270-L280)。注意 `input.slice_rows(0, m_pos_encoding->input_width())` 只读位置行，方向行被忽略；输出 `output` 由 `m_density_model` 填充（密度特征，渲染侧再取第 0 维并过激活）。

`density_forward()` / `density_backward()`（[nerf_network.h:282-355](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L282-L355)）是带反向的版本，供需要密度梯度的地方（如相机位姿优化、网格平滑）使用——它们同样只经过位置编码与密度网络。

**与完整前向的一致性。** 把 `density()` 与 `forward_impl` 对照：前者跑 `pos_encoding → density_network`，后者前半段也是这两步。因此 `density()` 产出的密度，与完整双头前向里被 `extract_density` 抽到第 4 维的 \(\sigma\)，来自同一组计算、同一组权重，数值一致。这正是密度网格能可靠地代表「网络当前认为哪里有密度」的根本原因。

#### 4.3.4 代码实践

**实践目标**：确认「密度专用前向」与「完整双头前向」用的是同一套密度权重，且被多个子系统复用。

**操作步骤**：

1. 在 [nerf_network.h 构造函数](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L77-L101) 找到 `m_density_model = std::make_shared<NetworkWithInputEncoding<T>>(m_pos_encoding, m_density_network);`（L100），确认它复用的是 `m_pos_encoding` 和 `m_density_network` 这两个**已有**成员（不是新建权重）。
2. 跳到 `set_params_impl`（[L357-L372](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L357-L372)），观察第一行 `m_density_model->set_params(params, inference_params, gradients);`——它和后续 density/rgb 网络用的是**同一段 `params` 指针**。
3. 在 `src/testbed_nerf.cu` 用 Grep 搜索 `->density(`，列出所有调用点：密度网格更新（L2570）、网格提取（L3430）。

**需要观察的现象**：`m_density_model` 与完整网络共享参数指针；`density()` 至少被「密度网格」和「网格提取」两个不同子系统调用。

**预期结果**：你能解释「为什么单独造一个 `m_density_model` 而不直接在 `forward_impl` 里加个开关」——因为 `NetworkWithInputEncoding` 是 tiny-cuda-nn 提供的现成「编码+MLP」打包器，复用它能让密度专用前向也享受 JIT 融合（`set_jit_fusion`）和统一参数管理，而不必在双头前向里塞条件分支。

#### 4.3.5 小练习与答案

**练习 1**：`density()` 为什么要求输入必须是列主序（CM）格式，而 `forward_impl` 没有这个限制？

**参考答案**：`density()` 走 `m_density_model`（`NetworkWithInputEncoding`）这条路径，该路径的实现（以及调用它的密度网格/网格提取代码）假定输入为列主序以便高效访存；而 `forward_impl` 手动管理多个 `GPUMatrixDynamic` 切片与布局（AoS/RM 都支持），通过 `layout()` 和 `stride()` 适配不同布局，因此不强制 CM。这是两条路径在工程上的取舍差异。

**练习 2**：如果训练中只更新了密度头权重、没更新颜色头权重（假设），`density()` 的输出会和之前不同吗？

**参考答案**：会不同。因为 `m_density_model` 与完整网络**共享同一块参数显存**（`set_params_impl` 把同一指针交给两者），密度头权重的任何更新都立刻对 `density()` 可见。这正是「共享权重」设计的好处：无需同步、永远一致。反过来也意味着，密度网格反映的就是网络当前最新的密度输出。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一个完整的「源码阅读 + 配置对照」任务。

**任务**：用 `configs/nerf/base.json` 的真实参数，手工填写一张「双头结构数据流表」，并在源码中为每一格找到证据。

**操作步骤**：

1. 打开 [configs/nerf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json)，读出四组件默认参数：
   - `encoding`：HashGrid, `n_levels=8, n_features_per_level=4, log2_hashmap_size=19, base_resolution=16`（L23-L29）；
   - `network`（密度头）：FullyFusedMLP, `n_neurons=64, n_hidden_layers=1`，无 `n_output_dims` → 默认 16（L30-L36）；
   - `dir_encoding`：SphericalHarmonics degree 4（3 维）+ Identity（L37-L49）；
   - `rgb_network`（颜色头）：FullyFusedMLP, `n_neurons=64, n_hidden_layers=2`（L50-L56）。

2. 在 [reset_network 的 NeRF 分支](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4266-L4310)（`src/testbed.cu` L4266-L4310）确认这四块 JSON 如何被传入 `NerfNetwork` 构造函数（L4282-L4293），并注意启动日志会打印 `Density model:` 与 `Color model:` 两行（L4301-L4310），分别描述两个头。

3. 填写下面这张表（每行都要能在 `forward_impl` 或配置里找到依据）：

   | 阶段 | 组件 | 输入来源 | 输出去向 | 配置/默认值 |
   | --- | --- | --- | --- | --- |
   | 位置编码 | `m_pos_encoding` | 输入第 0–2 行 | `density_network_input` | HashGrid 8×4, T=2¹⁹ |
   | 密度头 | `m_density_network` | `density_network_input` | `rgb_network_input` 前 16 行 | FullyFusedMLP 64×1, 出 16 维 |
   | 方向编码 | `m_dir_encoding` | 输入第 4–6 行 | `rgb_network_input` 后段 | SH degree4 |
   | 颜色头 | `m_rgb_network` | 整个 `rgb_network_input` | 输出前 3 维 RGB | FullyFusedMLP 64×2, 出 3 维 |
   | 密度提取 | `extract_density` 核 | 密度特征第 0 维 | 输出第 4 维（下标 3） | — |

4. 最后回答一个串联问题：**密度网格（u4-l5）更新时调用 `density()`，它跑的是上表哪几行？跳过了哪几行？为什么这样能省算力？**

**预期结果**：你能完整复述「输入 → 位置编码 → 密度头 →（密度特征复用）→ 拼接方向编码 → 颜色头 → RGB，σ 由 extract_density 补写」的全链路，并解释密度网格只跑前两行（位置编码 + 密度头）、跳过方向编码与颜色头，因此每步比完整前向便宜得多。

---

## 6. 本讲小结

- `NerfNetwork` 把 NeRF 实现成「双头」结构：**密度头**（位置→密度特征）+ **颜色头**（密度特征‖方向→RGB），外壳仍是一个输出 4 维 \([R,G,B,\sigma]\) 的普通 `Network`。
- 四个组件 `pos_encoding / density_network / dir_encoding / rgb_network` 与 `configs/nerf/base.json` 的四块一一对应；密度头默认输出 16 维特征，**第 0 维即体密度 \(\sigma\)**，全部 16 维送给颜色头当条件。
- `forward_impl` 里位置走两段、方向走半段；密度特征与方向特征在同一个 `rgb_network_input` 缓冲里前后拼接共享，省去一次显存拷贝；RGB 由颜色头写出，\(\sigma\) 由 `extract_density` 核补写到输出第 4 维。
- 反向传播（`backward_impl`）按颜色头→方向编码→密度头→位置编码的逆序回传，`add_density_gradient` 负责把 \(\sigma\) 的损失梯度叠回密度特征。
- `density()` / `density_forward()` 是「只算密度」的捷径，通过 `m_density_model`（复用 `pos_encoding`+`density_network`、**共享同一块参数显存**）服务密度网格、网格提取、相机优化等只需密度的子系统，跳过颜色头以省算力。
- JIT 设备函数（`generate_device_function`）把四步融合成一个端到端内核，其返回值 `{rgb[0],rgb[1],rgb[2],density_feat[0]}` 直观体现了「RGB 来自颜色头、σ 来自密度特征第 0 维」。

---

## 7. 下一步学习建议

本讲只讲了「网络长什么样」。接下来的三讲分别回答「它怎么被用起来」：

- **u4-l3 NeRF 光线步进与体渲染**：看 `render_nerf` 如何沿光线采样、把网络的 \([R,G,B,\sigma]\) 用 alpha 合成（transmittance 累积）变成像素颜色，以及密度网格位图如何让光线跳过空区域。本讲的 `forward_impl` / `density()` 正是渲染管线每步调用的核心。
- **u4-l4 NeRF 训练循环**：看 `train_nerf_step` 如何批量采样光线、计算损失并反向——本讲的 `backward_impl` 是那里反向传播的落脚点。
- **u4-l5 密度网格与空区域跳过**：深入 `update_density_grid_nerf` 如何用 `density()` 在网格上采样、压成位图，是本讲「密度专用前向」的最大消费者。

若对「组件如何由 JSON 构造、为何要 16 对齐」还想再夯实，可回看 **u3-l3 网络构建与 FullyFusedMLP**；对哈希编码本身的数学则看 **u3-l2 多分辨率哈希编码**。
