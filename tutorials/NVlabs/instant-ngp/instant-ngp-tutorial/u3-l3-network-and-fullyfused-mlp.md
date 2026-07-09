# 网络构建与 FullyFusedMLP

## 1. 本讲目标

本讲紧接 u3-l1（`reset_network` 与五大模型对象）。在 u3-l1 里我们已知 `reset_network()` 会调用 tiny-cuda-nn 的工厂构造 `m_loss / m_optimizer / m_encoding / m_network / m_trainer` 五个对象，但故意留下一个问题：**`m_network` 这个 MLP 到底是怎么造出来的？为什么 NeRF 和另外三种模式走的是两条不同的路？**

读完本讲你应当能够：

- 说清 `reset_network` 中「NeRF 分支」与「其余三种模式分支」的差异，以及各自用了哪个网络包装器。
- 解释 `FullyFusedMLP` / `MegakernelMLP` 为什么要求 **16 维对齐**，而通用网络只要 **8 对齐**。
- 读懂 `network_config` 里 `n_neurons` / `n_hidden_layers` / `activation` / `output_activation` 的作用。
- 理解 `NetworkWithInputEncoding` 如何把「输入编码」和「MLP」打包成一个可被 `Trainer` 统一训练的 `Network` 对象。

> 边界提示：本仓库是**应用层**，`FullyFusedMLP` / `MegakernelMLP` / `NetworkWithInputEncoding` / `create_encoding` / `create_network` / `minimum_alignment` / `next_multiple` 的真正实现都在外部依赖 **tiny-cuda-nn** 中。本讲只引用 instant-ngp 仓库内**真实存在**的代码，对 tiny-cuda-nn 内部只描述其对外契约，不编造其源码。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是「对齐」（alignment）。** GPU 上做矩阵乘法时，为了让一次内存读取刚好填满一个缓存行或一个半精度（fp16）向量化访问单元，常常要求矩阵的某一维是某个数的倍数。比如要求「输入宽度是 16 的倍数」。如果编码输出的真实宽度是 30，就会被 **padding（补齐）** 成 32 再喂给 MLP——多出来的 2 维只是占位，不携带信息，但让内核能跑在最快路径上。本讲会反复出现 `padded_output_width`（补齐后的宽度）这个词。

**什么是 FullyFusedMLP。** 普通的 MLP 实现是「一层一个 CUDA 内核」：先算第一层写回显存，再读出来算第二层……每层之间都要读写中间结果。`FullyFusedMLP` 是 tiny-cuda-nn 提供的一种**全融合**实现：把整个 MLP 的前向（以及反向）融合进**单个 CUDA 内核**，中间激活值自始至终留在寄存器/共享内存里，不落盘显存。这就是 instant-ngp 能做到秒级训练的关键之一。`MegakernelMLP` 是它的 JIT 融合变体（u8-l2 会详谈）。

**什么是 NetworkWithInputEncoding。** `Trainer` 只认一个 `Network` 对象去前向/反向/更新参数。但我们的模型其实是「编码 + MLP」两段。`NetworkWithInputEncoding` 是一个**包装器**：它把一个 `Encoding` 和一个 `Network`（MLP）组合成一个新的 `Network`，对外暴露统一的接口，内部先把输入过编码、再过 MLP。这样 `Trainer` 就能一次把哈希表参数和 MLP 权重一起训练。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `reset_network()` 所在地，包含两个构造分支与 alignment 判定 |
| [include/neural-graphics-primitives/nerf_network.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h) | `NerfNetwork` 双头网络的声明与构造，alignment 在这里被显式施加 |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | `NetworkDims` 结构体、`reset_network` 声明、`CudaDevice` 内嵌类 |
| [configs/nerf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json) | NeRF 模式默认网络配置（`network` + `rgb_network` 两套 MLP） |
| [configs/sdf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json) · [configs/image/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/image/base.json) · [configs/volume/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/volume/base.json) | 另外三种模式的默认配置，用于横向对比 |
| [src/testbed_nerf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu) · [src/testbed_sdf.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu) · [src/testbed_image.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu) · [src/testbed_volume.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu) | 各模式的 `network_dims_*()`，决定输入/输出维度 |

## 4. 核心概念与源码讲解

### 4.1 网络构造分支：NeRF 走双头，其余走单头

#### 4.1.1 概念说明

四种基元对网络的需求并不相同：

- **NeRF** 要从「位置」预测「密度」，再从「方向 + 密度特征」预测「颜色」——这是**两段式（双头）**结构，所以需要一个专门的 `NerfNetwork` 类把两个 MLP 和两个编码组织起来。
- **SDF / Image / Volume** 只需要「坐标 → 标量或向量」的**单段式** MLP：SDF 输出 1 维距离、Image 输出 3 维 RGB、Volume 输出 4 维 RGBA。它们共用一个通用包装器 `NetworkWithInputEncoding`。

`reset_network()` 用 `m_testbed_mode == ETestbedMode::Nerf` 这一个判断把两条路分开。注意：**分支的依据是模式，不是配置文件里的 `otype`**——配置里的 `otype` 决定的是「用哪种 MLP 内核」，而模式决定的是「用哪个网络包装器」。

#### 4.1.2 核心流程

`reset_network()` 在网络构造部分的核心流程（去掉无关细节）如下：

```
auto dims = network_dims();          // 按模式取 n_input / n_output / n_pos
m_loss     = create_loss(loss_config)
m_optimizer= create_optimizer(optimizer_config)

if (m_testbed_mode == Nerf) {
    // —— NeRF 分支：构造双头 NerfNetwork ——
    for (each device)
        device.set_nerf_network(make_shared<NerfNetwork>(
            dims.n_pos, n_dir_dims, n_extra_dims, dims.n_pos+1,
            encoding_config, dir_encoding_config,
            network_config, rgb_network_config))   // 注意：传了两套 MLP 配置
    m_network  = m_nerf_network = primary_device().nerf_network()
    m_encoding = m_nerf_network->pos_encoding()
    // 额外构造 distortion_map 副模型
} else {
    // —— 其余分支：构造单头 NetworkWithInputEncoding ——
    alignment = (otype ∈ {FullyFusedMLP, MegakernelMLP}) ? 16 : 8
    if (encoding otype == "Takikawa")
        m_encoding = new TakikawaEncoding(...)       // SDF 专用八叉树编码
    else
        m_encoding = create_encoding(dims.n_input, encoding_config)
    for (each device)
        device.set_network(make_shared<NetworkWithInputEncoding>(
            m_encoding, dims.n_output, network_config))
    m_network = primary_device().network()
}

set_jit_fusion(m_jit_fusion)                         // 对每个 device 设置 JIT 融合开关
m_trainer  = make_shared<Trainer>(m_network, m_optimizer, m_loss, m_seed)
m_training_step = 0                                   // 从头训练
```

两条分支最后都汇合到同一个 `m_trainer`：无论双头还是单头，对 `Trainer` 而言都只是一个 `Network` 对象。这是本节最关键的设计——**包装器把差异藏起来，训练循环只写一份**。

#### 4.1.3 源码精读

先看分支前的准备工作。`reset_network` 先从 `m_network_config` 取出四大块配置，再调用 `network_dims()` 拿到当前模式的维度：

[src/testbed.cu:4192-4206](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4192-L4206) — 取出 `encoding / loss / optimizer / network` 四块配置，并调用 `network_dims()`。其中 `network_dims()` 是个按模式分发的 switch：

[src/testbed.cu:4150-4158](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4150-L4158) — `NetworkDims` 三字段（`n_input / n_output / n_pos`）按模式取值。四个模式各自的取值如下表，来源是各 `network_dims_*()` 函数：

| 模式 | `n_input` | `n_output` | `n_pos` | 出处 |
| --- | --- | --- | --- | --- |
| Nerf | `sizeof(NerfCoordinate)/4` | 4 (RGBA) | `sizeof(NerfPosition)/4` | [testbed_nerf.cu:55-61](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_nerf.cu#L55-L61) |
| Sdf | 3 | 1 (距离) | 3 | [testbed_sdf.cu:45-51](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L45-L51) |
| Image | 2 | 3 (RGB) | 2 | [testbed_image.cu:31-37](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_image.cu#L31-L37) |
| Volume | 3 | 4 (RGBA) | 3 | [testbed_volume.cu:36-42](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_volume.cu#L36-L42) |

`NetworkDims` 结构体本身定义在 [include/neural-graphics-primitives/testbed.h:313-317](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L313-L317)。

接着是 **NeRF 分支**。注意它给 `NerfNetwork` 传了**两套** MLP 配置（`network_config` 当密度网络、`rgb_network_config` 当颜色网络）和**两套**编码配置（`encoding_config` 当位置编码、`dir_encoding_config` 当方向编码）：

[src/testbed.cu:4274-4296](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4274-L4296) — 取出 `dir_encoding` 与 `rgb_network` 配置，为每块 GPU 构造一个 `NerfNetwork`，并把主设备的网络同时赋给 `m_network` 和 `m_nerf_network`。这里 `dims.n_pos + 1` 那个 `+1` 来自 `NerfCoordinate` 里的 `dt` 成员（代码注释里写着 `HACKY`），是方向向量在输入里的起始偏移。

NeRF 分支还会额外构造一个 `distortion_map`（可训练畸变图）副模型，详见 [src/testbed.cu:4312-4327](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4312-L4327)——它有自己的 `Trainer`，和主网络并行训练。这块属于相机自标定（u8-l3），本讲只点到为止。

再看 **else 分支**（SDF / Image / Volume 共用）：

[src/testbed.cu:4328-4374](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4328-L4374) — 先算 `alignment`（见 4.2 节），再按编码 `otype` 分两路：`Takikawa` 走八叉树编码（SDF 专用），其余走通用 `create_encoding`；最后用 `NetworkWithInputEncoding` 把编码与 MLP 打包。关键三行：

```cpp
// testbed.cu:4354 —— 通用编码（2 参数版，未显式传 alignment）
m_encoding.reset(create_encoding<network_precision_t>(dims.n_input, encoding_config));
// testbed.cu:4363 —— 把编码 + MLP 打包成单一 Network
device.set_network(std::make_shared<NetworkWithInputEncoding<network_precision_t>>(
    m_encoding, dims.n_output, network_config));
// testbed.cu:4366
m_network = primary_device().network();
```

两条分支汇合后做三件收尾事：

[src/testbed.cu:4376-4388](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4376-L4388) — `set_jit_fusion` 对每块 GPU 设置 JIT 融合开关；统计 `n_network_params`（注意它用 `m_network->n_params() - n_encoding_params`，把编码参数从网络参数里扣出来单独报告）；构造 `m_trainer`；把 `m_training_step` 归零（即从头训练，这正是 u3-l1 强调的「reset = 从头训」）。

> 旁注：构造网络时会对**每块 GPU** 各造一份（`for (auto& device : m_devices)`），主设备的那份赋给 `m_network`。这与多 GPU 支持（u8-l1）相关，本讲把它当作「为每块设备各造一份网络副本」即可。`set_network` / `set_nerf_network` 的声明见 [include/neural-graphics-primitives/testbed.h:1144-1145](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1144-L1145)。

#### 4.1.4 代码实践

**实践目标**：把「模式 → 维度 → 分支 → 网络包装器」这条链手动走一遍，验证你读懂了分发逻辑。

**操作步骤**：

1. 打开四个配置文件 [configs/nerf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json)、[configs/sdf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json)、[configs/image/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/image/base.json)、[configs/volume/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/volume/base.json)。
2. 对照上方的维度表，填出下表（在脑中或纸上）：

| 模式 | 进入哪个分支 | 网络包装器 | 配置里有几套 MLP（`network`/`rgb_network`） | `n_output` |
| --- | --- | --- | --- | --- |

**需要观察的现象**：NeRF 配置里有 `network` **和** `rgb_network` 两套 MLP（[configs/nerf/base.json:30-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L30-L36) 与 [configs/nerf/base.json:50-56](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L50-L56)）；而 SDF/Image/Volume 的配置里**只有** `network` 一套 MLP。

**预期结果**：NeRF → NeRF 分支 → `NerfNetwork`（双头，两套 MLP，`n_output=4`）；SDF/Image/Volume → else 分支 → `NetworkWithInputEncoding`（单头，一套 MLP，`n_output` 分别为 1/3/4）。这是一个纯源码阅读型实践，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：如果用户给 NeRF 模式加载了一份**只有 `network`、没有 `rgb_network`** 的配置，会发生什么？

**参考答案**：`reset_network` 的 NeRF 分支在 [testbed.cu:4275](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4275) 直接取 `config["rgb_network"]`。若该键缺失，nlohmann::json 会插入一个 `null`，随后 `NerfNetwork` 构造函数里 `minimum_alignment(rgb_network)`（[nerf_network.h:83](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L83)）读取 `otype` 时会抛 JSON 异常。所以 NeRF 必须同时提供两套 MLP 配置——这也是 `configs/nerf/base.json` 写了两套的原因。

**练习 2**：为什么 `network_dims()` 要单独返回 `n_pos`，而不直接用 `n_input`？

**参考答案**：NeRF 模式下 `n_input` 包含位置、方向、`dt` 等多个字段（`sizeof(NerfCoordinate)/4`），但**只有位置**那几维需要过哈希编码。`n_pos` 专门告诉编码「前 `n_pos` 维是位置」。其余三种模式 `n_pos == n_input`，所以看不出差别；差别只在 NeRF 上体现。

### 4.2 FullyFusedMLP 与 16/8 对齐

#### 4.2.1 概念说明

「对齐」在 4.2 前置知识里已解释：要求 MLP 某些层的宽度是 N 的倍数。本节回答两个问题：

1. **判定条件**：`alignment` 取 16 还是 8，由什么决定？
2. **原因**：为什么 `FullyFusedMLP` / `MegakernelMLP` 偏要 16，而别的网络只要 8？

先给结论：**当 MLP 的 `otype` 是 `FullyFusedMLP` 或 `MegakernelMLP` 时，对齐数取 16；否则取 8。** 这个判定在仓库里出现两次——一次在 `NerfNetwork` 构造函数里（针对密度网络的输入编码），一次在 `reset_network` 的 else 分支里。

至于「为什么是 16」：`FullyFusedMLP` 是手写的全融合内核，它把激活值和权重按 **16 个一组**的分块（tile）来处理——每个线程块一次搬运 16 个元素，权重也按 16 宽的布局存放，以匹配半精度向量化访存与寄存器分配。这就要求**每一层的输入宽度必须是 16 的倍数**，否则分块访存会越界或低效。`MegakernelMLP` 是同体系的 JIT 融合变体，沿用同样的 16 分块约束。而通用网络（tiny-cuda-nn 中基于 CUTLASS GEMM 的实现）走的是更通用的矩阵乘路径，只需 **8 元素对齐**（半精度访存的自然对齐要求）即可。

> 边界说明：上述「16 分块 / 8 对齐」的内核实现细节属于 tiny-cuda-nn。本仓库只做一件事——**遵守**这个约束：把编码输出宽度 padding 到 `alignment` 的倍数，让全融合内核能跑在快路径上。本仓库的代码不解释「为什么是 16」，只机械地判定和补齐。

#### 4.2.2 核心流程

对齐在两条分支里的施加方式不同：

- **NeRF 分支（显式施加）**：`NerfNetwork` 构造函数在创建位置编码时，把 `16u`/`8u` 作为第 3 个参数传给 `create_encoding`；在创建颜色网络时，调用 tiny-cuda-nn 的 `minimum_alignment(rgb_network)` 自动判定；再用 `next_multiple(..., rgb_alignment)` 把颜色网络输入宽度补齐到对齐数的倍数。
- **else 分支（委托施加）**：`reset_network` 算出一个 `alignment` 局部变量，但**在本仓库可见代码里它并未被传给** `create_encoding`（用的是 2 参数版）或 `NetworkWithInputEncoding`。真正的对齐由 tiny-cuda-nn 的 `NetworkWithInputEncoding` 构造函数内部依据 `network_config` 的 `otype` 自行施加（其内部逻辑等价于同样的 `minimum_alignment`）。这个 `alignment` 局部变量在 else 分支里实际上是**计算了却未被直接使用**的——这是一处值得注意的代码现象，读者可自行 grep 验证。

#### 4.2.3 源码精读

先看 else 分支里的判定（这就是本讲实践任务要找的那段）：

[src/testbed.cu:4329-4333](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4329-L4333) — `alignment` 的三元判定：

```cpp
uint32_t alignment = network_config.contains("otype") &&
        (equals_case_insensitive(network_config["otype"], "FullyFusedMLP") ||
         equals_case_insensitive(network_config["otype"], "MegakernelMLP")) ?
    16u :
    8u;
```

判定逻辑一句话：**`otype` 是 `FullyFusedMLP` 或 `MegakernelMLP` → 16，否则 → 8。** `equals_case_insensitive` 与 `minimum_alignment` 都是 tiny-cuda-nn 提供的工具函数。

再看 NeRF 分支里 `NerfNetwork` 如何**显式**施加对齐：

[include/neural-graphics-primitives/nerf_network.h:81-101](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L81-L101) — 构造函数。关键四行：

```cpp
// nerf_network.h:82 —— 位置编码按密度网络 otype 判对齐：FullyFused/Megakernel→16，否则 8
m_pos_encoding.reset(create_encoding<T>(n_pos_dims, pos_encoding,
    density_network.contains("otype") &&
    (equals_case_insensitive(density_network["otype"], "FullyFusedMLP") ||
     equals_case_insensitive(density_network["otype"], "MegakernelMLP")) ? 16u : 8u));
// nerf_network.h:83 —— 颜色网络用 tiny-cuda-nn 的 minimum_alignment 自动判
uint32_t rgb_alignment = minimum_alignment(rgb_network);
// nerf_network.h:84 —— 方向编码按颜色网络的对齐数 padding
m_dir_encoding.reset(create_encoding<T>(m_n_dir_dims + m_n_extra_dims, dir_encoding, rgb_alignment));
// nerf_network.h:93 —— 颜色网络输入宽度 = density输出 + dir编码输出，补齐到 rgb_alignment 的倍数
m_rgb_network_input_width = next_multiple(
    m_dir_encoding->padded_output_width() + m_density_network->padded_output_width(), rgb_alignment);
```

可以看到三处对齐施加点：位置编码（行 82）、方向编码（行 84）、颜色网络输入宽度（行 93）。`padded_output_width()` 就是「补齐到对齐数后的输出宽度」，`next_multiple(x, a)` 即 \(\lceil x/a \rceil \times a\)。

最后看默认配置怎么体现这一点。四份 `base.json` 的 `network` 块**全都**用 `FullyFusedMLP`：

- [configs/nerf/base.json:30-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L30-L36) — `otype: FullyFusedMLP`，`n_neurons: 64`，`n_hidden_layers: 1`（密度网络）
- [configs/nerf/base.json:50-56](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L50-L56) — 颜色网络同为 `FullyFusedMLP`，`n_hidden_layers: 2`
- [configs/sdf/base.json:30-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L30-L36) · [configs/image/base.json:30-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/image/base.json#L30-L36) · [configs/volume/base.json:30-36](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/volume/base.json#L30-L36) — 三者都是 `FullyFusedMLP`，`n_neurons: 64`，`n_hidden_layers: 2`

也就是说，开箱即用的四种模式**全部**命中 `alignment = 16`。要触发 `alignment = 8`，得把 `otype` 改成 `CutlassMLP` 这类通用网络。

#### 4.2.4 代码实践

**实践目标**：亲手定位 alignment 判定条件，并验证对默认配置而言编码输出宽度确实是 16 的倍数。

**操作步骤**：

1. 在 [src/testbed.cu:4329-4333](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4329-L4333) 与 [include/neural-graphics-primitives/nerf_network.h:82-84](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L82-L84) 找到判定条件，抄下：「`otype` 为 FullyFusedMLP/MegakernelMLP → 16，否则 → 8」。
2. 用 `grep -n alignment src/testbed.cu` 确认 else 分支里 `alignment` 这个局部变量**只在第 4329 行出现一次**（即被赋值后未被直接使用），验证 4.2.2 节的「委托施加」说法。
3. 手算默认 SDF 配置的编码输出宽度：`n_levels=16` × `n_features_per_level=2` = 32（见 [configs/sdf/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L23-L29)）。32 是 16 的倍数，所以 padding 不增加任何维度。
4. 再手算 NeRF 密度编码：`n_levels=8` × `n_features_per_level=4` = 32（见 [configs/nerf/base.json:23-29](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L23-L29)），同样是 16 的倍数。

**需要观察的现象**：两种默认配置的编码输出宽度都恰好是 32，无需额外 padding 即满足 16 对齐——这不是巧合，而是 `n_features_per_level` 取 2 或 4（本身就是 2 的幂）配合 `n_levels` 凑出的结果。

**预期结果**：判定条件 = `otype ∈ {FullyFusedMLP, MegakernelMLP}`；默认配置下编码输出宽度均为 16 的倍数。

**为什么 16 vs 8（本实践的核心解释）**：`FullyFusedMLP` / `MegakernelMLP` 是全融合内核，按 16 元素分块处理激活与权重、用 16 宽布局存放权重以匹配半精度向量化访存，故每层输入宽度必须是 16 的倍数；通用网络（如 `CutlassMLP`）走 CUTLASS GEMM 通用路径，只需 8 元素对齐。内核实现在 tiny-cuda-nn（本快照未检出的子模块），本仓库只负责按此约束 padding。

#### 4.2.5 小练习与答案

**练习 1**：把 [configs/sdf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json) 的 `network.otype` 从 `FullyFusedMLP` 改成 `CutlassMLP`，`alignment` 会怎么变？编码输出宽度会变吗？

**参考答案**：`alignment` 从 16 变 8（见 [testbed.cu:4329-4333](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4329-L4333)）。编码输出真实宽度仍是 32，32 既是 16 也是 8 的倍数，所以 `padded_output_width` 不变。只有当真实宽度是 8 的倍数但非 16 的倍数（例如 24）时，从 16 降到 8 才会减少 padding 维度。

**练习 2**：`NetworkWithInputEncoding`（else 分支）明明没有显式接收 `alignment`，为什么最终网络仍能正确对齐？

**参考答案**：因为 `NetworkWithInputEncoding` 的构造函数（tiny-cuda-nn 实现）接收了 `network_config`，它会内部依据 `network_config["otype"]` 调用等价于 `minimum_alignment` 的逻辑自行判定并 padding。本仓库的 `alignment` 局部变量（[testbed.cu:4329](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4329)）在 else 分支里属于「计算了却未直接使用」——对齐被委托给库内部完成。

### 4.3 NetworkWithInputEncoding：把编码与 MLP 组合成一个 Network

#### 4.3.1 概念说明

回顾 u3-l1：`Trainer` 只持有一个 `m_network`，它对 `m_network` 调用前向、算损失、反向、用 `m_optimizer` 更新参数。但我们的真实模型是「输入编码（如 HashGrid，含可学习哈希表）+ MLP」两段。如果 `Trainer` 只看到 MLP，那哈希表的参数就没人训练了。

`NetworkWithInputEncoding` 解决的就是这个「接口不匹配」：它是一个**适配器**，把一个 `Encoding` 和一个 `Network`（MLP）包成一个新的 `Network`：

- 对外：表现为一个普通的 `Network`，输入是原始坐标（如 3 维位置），输出是 MLP 的结果。
- 对内：前向时先调用 `Encoding` 把坐标编码成高维特征，再喂给 MLP；参数空间是「编码参数 + MLP 参数」的拼接，因此一次反向就能同时更新哈希表和 MLP 权重。

NeRF 模式其实也用了同样的思路——`NerfNetwork` 内部为了单独采样密度，额外用 `NetworkWithInputEncoding` 把「位置编码 + 密度 MLP」包成一个 `m_density_model`（见下文）。所以 `NetworkWithInputEncoding` 是本项目里「编码+MLP」组合的通用积木。

#### 4.3.2 核心流程

- **else 分支**：`reset_network` 先造好 `m_encoding`，再把 `(m_encoding, dims.n_output, network_config)` 三元组交给 `NetworkWithInputEncoding` 构造。包装器内部会按 `network_config` 造出 MLP（含 alignment padding），并把编码参数与 MLP 参数拼到同一块显存。
- **NeRF 分支**：`NerfNetwork` 构造函数内部，把 `m_pos_encoding`（位置编码）和 `m_density_network`（密度 MLP）同样包成一个 `m_density_model = NetworkWithInputEncoding(...)`，用于只查密度的场景（密度网格采样，u4-l5）。完整的前向（位置→密度→颜色）则由 `NerfNetwork::forward_impl` 自己编排，不走这个包装器。

#### 4.3.3 源码精读

else 分支的打包这一行是本节核心：

[src/testbed.cu:4362-4366](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4362-L4366) — 用 `NetworkWithInputEncoding` 把 `m_encoding` 与 `network_config` 描述的 MLP 打包，输出维度取 `dims.n_output`：

```cpp
for (auto& device : m_devices) {
    device.set_network(std::make_shared<NetworkWithInputEncoding<network_precision_t>>(
        m_encoding, dims.n_output, network_config));
}
m_network = primary_device().network();
```

注意 `m_encoding` 是**先单独构造**的（[testbed.cu:4354](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4354)），再以 `shared_ptr` 形式喂给包装器——这意味着同一份编码对象被包装器托管，外部持有的 `m_encoding` 与包装器内部的编码是同一个。

NeRF 分支里对等的那块积木在 `NerfNetwork` 构造函数末尾：

[include/neural-graphics-primitives/nerf_network.h:100](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L100) — `m_density_model = std::make_shared<NetworkWithInputEncoding<T>>(m_pos_encoding, m_density_network);`，把位置编码与密度 MLP 组合成一个可独立前向的子模型。它在 [nerf_network.h:270-280](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L270-L280) 的 `density()` 方法里被用来只查密度（密度网格更新时会大量调用，详见 u4-l5）。

参数统一性体现在 `NerfNetwork::n_params()`：

[include/neural-graphics-primitives/nerf_network.h:388-390](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L388-L390) — `n_params()` 把四个组件（位置编码、密度网络、方向编码、颜色网络）的参数数全部相加：

```cpp
size_t n_params() const override {
    return m_pos_encoding->n_params() + m_density_network->n_params()
         + m_dir_encoding->n_params() + m_rgb_network->n_params();
}
```

这正是「包装器对外暴露统一参数空间」的含义：`Trainer` 只看到 `m_network->n_params()` 这一个总数，分配一块连续显存，再由 `set_params_impl`（[nerf_network.h:357-372](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L357-L372)）按偏移切给四个子组件。`NetworkWithInputEncoding` 对单段式模型做的是同样的事，只是组件更少（一个编码 + 一个 MLP）。

顺带一提，`NerfNetwork` 对外声明的输出宽度是 4（RGBA），且 `required_input_alignment` 返回 1（无需外部对齐，因为编码已吸收对齐要求）：

[include/neural-graphics-primitives/nerf_network.h:392-402](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L392-L402) — `padded_output_width() = max(rgb_network padded, 4)`，`output_width() = 4`。
[include/neural-graphics-primitives/nerf_network.h:408-410](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L408-L410) — `required_input_alignment() = 1`，注释写明「No alignment required due to encoding」。

#### 4.3.4 代码实践

**实践目标**：验证「`Trainer` 只需要一个 `m_network` 就能同时训练编码参数与 MLP 参数」这一设计。

**操作步骤**：

1. 在 [src/testbed.cu:4379-4383](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4379-L4383) 看到 `n_network_params = m_network->n_params() - n_encoding_params`，并看到 `m_trainer` 只接收 `m_network` 一个模型对象。
2. 在 [include/neural-graphics-primitives/nerf_network.h:388-390](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L388-L390) 看到 `n_params()` 把编码与 MLP 参数相加；在 [nerf_network.h:357-372](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L357-L372) 看到 `set_params_impl` 用偏移把同一块参数内存切给各组件。
3. 回答下面的问题（见预期结果）。

**需要观察的现象**：`m_trainer` 的构造只传了 `m_network`，没有单独传 `m_encoding`；但 `m_network->n_params()` 已经把编码参数算进去了。

**预期结果**：因为 `NetworkWithInputEncoding`（以及 `NerfNetwork`）把编码参数和 MLP 参数拼进了**同一个参数向量**，`Trainer` 对这块连续显存做一次反向 + 一次优化器更新，就同时更新了哈希表与 MLP 权重。这就是「包装器把差异藏起来，训练循环只写一份」的实现机理。

**待本地验证**：若你在本地编译并能跑起来，可在 `reset_network` 末尾打印 `n_encoding_params` 与 `n_network_params`（[testbed.cu:4381](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4381) 的 `tlog` 已经会打印 `total_encoding_params` 与 `total_network_params`），观察 SDF 模式下编码参数（哈希表）远多于 MLP 参数的现象。

#### 4.3.5 小练习与答案

**练习 1**：else 分支里 `m_encoding` 是先单独 `create_encoding` 出来再交给 `NetworkWithInputEncoding` 的，为什么不直接让 `NetworkWithInputEncoding` 自己造编码？

**参考答案**：因为同一个编码对象还要被外部直接使用——例如 `m_encoding = primary_device().network()` 之外，`reset_network` 后续要读 `m_encoding->n_params()`、`m_encoding->padded_output_width()` 来统计与日志（[testbed.cu:4368-4373](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4368-L4373)）。此外 Takikawa 编码是 instant-ngp 自己的类（`new TakikawaEncoding`），不走 `create_encoding` 工厂，必须在外面造好再传入。

**练习 2**：NeRF 的 `NerfNetwork` 已经自己编排了双头前向，为什么还要再包一个 `m_density_model`（`NetworkWithInputEncoding`）？

**参考答案**：因为密度网格更新（u4-l5）只需要在大量空间点上查询密度，不需要颜色。`m_density_model` 把「位置编码 + 密度 MLP」包成一个可独立前向的子模型，让 `density()` 方法（[nerf_network.h:270-280](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L270-L280)）能直接批量查密度，避免走完整的双头前向浪费算力。这是同一套「编码+MLP」积木的复用。

## 5. 综合实践

设计一个把本讲三个模块串起来的小任务：**为 SDF 模式手算一次网络形状，并预测改 `otype` 的影响**。

1. **取维度**：从 [testbed_sdf.cu:45-51](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed_sdf.cu#L45-L51) 得 SDF 的 `n_input=3, n_output=1, n_pos=3`。
2. **取配置**：从 [configs/sdf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json) 得编码 `n_levels=16, n_features_per_level=2`，网络 `otype=FullyFusedMLP, n_neurons=64, n_hidden_layers=2`。
3. **判分支与对齐**：SDF 走 else 分支；`otype=FullyFusedMLP` → `alignment=16`（[testbed.cu:4329-4333](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4329-L4333)）。
4. **算编码输出宽度**：`16 × 2 = 32`，是 16 的倍数，`padded_output_width = 32`。
5. **画 MLP 形状**：`32 → 64 → 64 → 1`（输入 32，两个 64 神经元隐层，输出 1 维距离）。
6. **预测 tlog**：[testbed.cu:4370-4373](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4370-L4373) 会打印类似 `Model: 3--[HashGrid]-->32--[FullyFusedMLP(neurons=64,layers=4)]-->1`。
7. **改 otype 思考**：若把 `otype` 改成 `CutlassMLP`，`alignment` 降到 8；本例编码宽度 32 不受影响，但通用 MLP 速度更慢、且不再能用 `MegakernelMLP` 的 JIT 全融合路径（见 u8-l2）。

**待本地验证**：第 6 步的 tlog 实际输出需在本地编译运行后确认；形状推导（第 4、5 步）可纯靠源码阅读完成。

## 6. 本讲小结

- `reset_network` 用 `m_testbed_mode == Nerf` 把网络构造分成两条路：NeRF 造双头 `NerfNetwork`（两套 MLP + 两套编码），SDF/Image/Volume 造单头 `NetworkWithInputEncoding`（一套 MLP + 一套编码）。
- 分支依据是**模式**而非配置 `otype`；`otype` 决定的是用哪种 MLP 内核，模式决定的是用哪个网络包装器。
- `alignment` 取 16 还是 8 的判定条件是：`network_config["otype"]` 是否为 `FullyFusedMLP` 或 `MegakernelMLP`——是则 16，否则 8。判定在 [testbed.cu:4329-4333](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4329-L4333) 与 [nerf_network.h:82-84](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/nerf_network.h#L82-L84) 各出现一次。
- 16 对齐源自 `FullyFusedMLP`/`MegakernelMLP` 全融合内核的 16 元素分块与权重布局；通用网络只需 8 对齐。内核实现在 tiny-cuda-nn，本仓库只按此约束 padding。
- NeRF 分支**显式**施加对齐（位置编码、方向编码、颜色网络输入宽度三处）；else 分支则把对齐**委托**给 `NetworkWithInputEncoding` 内部，本地 `alignment` 变量计算后未被直接使用。
- `NetworkWithInputEncoding` 是「编码 + MLP」的适配器，把两者参数拼进同一块显存，使 `Trainer` 只需一个 `m_network` 就能同时训练哈希表与 MLP 权重；`NerfNetwork` 内部也用同款积木造了 `m_density_model` 供密度网格单独采样。

## 7. 下一步学习建议

- 想看 `NerfNetwork` 双头前向（位置→密度→颜色）的完整数据流与 `extract_density` 如何把密度塞进输出第 4 维，进入 **u4-l2 NerfNetwork 双头架构**。
- 想了解 `NerfNetwork::generate_device_function` 如何把编码与两个 MLP 拼成单个 JIT 设备函数（即 `MegakernelMLP` 的运行时编译机制），进入 **u8-l2 JIT 融合与全融合内核**。
- 想理解 `m_density_model` 被谁大量调用、密度网格如何用 EMA 跟踪网络输出，进入 **u4-l5 密度网格与空区域跳过**。
- 若你想动手改配置做对照实验（换 `otype`、调 `n_neurons`/`n_hidden_layers`），可先读 **u3-l4 编码方式对比实验**，那里有完整的配置切换方法论。
