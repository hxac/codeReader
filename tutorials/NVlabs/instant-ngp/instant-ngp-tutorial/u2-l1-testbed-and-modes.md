# Testbed 类与四种模式

## 1. 本讲目标

本讲是「核心架构」单元的第一篇。学完后你应当能够：

- 说清 `Testbed` 为什么被设计成一个承载全部状态的「巨型类」，以及它内部由哪几类成员拼装而成。
- 掌握 `ETestbedMode` 枚举的四个取值（`Nerf/Sdf/Image/Volume`）外加 `None` 哨兵值的切换语义。
- 读懂 `set_mode()` 如何在切换模式时把「模式专属状态」和「网络相关成员」一并清空、再重设默认值。
- 区分五个顶层网络成员 `m_loss / m_optimizer / m_encoding / m_network / m_trainer` 各自的职责，以及它们在 `reset_network()` 中如何被构造出来。

本讲只讲「类的骨架与模式分发」，不展开任何一种基元的具体训练/渲染算法——那是后续讲义的内容。

## 2. 前置知识

阅读本讲前，你需要已经建立以下认知（来自入门单元）：

- instant-ngp 围绕一个「上帝对象」`Testbed` 组织，其声明在 `testbed.h`，骨架实现在 `testbed.cu`，`m_testbed_mode` 是几乎所有分发逻辑的开关。
- 项目实现四种神经图形基元：NeRF（场景）、SDF（几何表面）、神经图像、神经体素，在源码中对应 `ETestbedMode` 的四种模式，由程序根据输入文件类型自动判别。
- 底层神经网络能力（MLP、哈希编码、优化器、损失、Trainer）来自外部依赖 tiny-cuda-nn；本仓库是应用层。
- `configs/` 目录按 `nerf/sdf/image/volume` 四个子目录存放网络 JSON 配置。

如果你对这些还不熟悉，建议先读 `u1-l1` 到 `u1-l5`。

补充两个本讲会用到的 C++ 概念：

- **`std::shared_ptr`（共享指针）**：一种带引用计数的智能指针，多个拥有者可共享同一对象，最后一个拥有者销毁时对象自动释放。Testbed 里几乎所有网络对象都用 `shared_ptr` 持有，因为同一份网络参数会被主设备和辅助设备、训练器和渲染器共同引用。
- **值初始化 `= {}`**：对一个结构体变量写 `x = {};` 会把它所有成员重置为默认值（指针置空、数值归零、容器清空）。`set_mode()` 大量使用这一写法来「一键清空」模式专属状态。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/neural-graphics-primitives/common.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h) | 定义 `ETestbedMode` 等全部枚举，是跨文件的「公共词汇表」。 |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | `Testbed` 类的完整声明：构造函数、`set_mode`、四个内嵌结构体 `m_nerf/m_sdf/m_image/m_volume`、五个网络成员、内嵌的 `CudaDevice`。 |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `Testbed` 的骨架实现：构造函数、`set_mode()`、`reset_network()`、`train()` 分发等共用逻辑。 |
| [src/common_host.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu) | `to_string(ETestbedMode)` 等工具函数，把模式枚举转成小写字符串，用于拼接 `configs/<mode>/` 路径。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**Testbed 类结构**、**ETestbedMode 与模式切换**、**模式专属状态**、**顶层网络成员**。

### 4.1 Testbed 类结构

#### 4.1.1 概念说明

`Testbed` 是整个程序唯一的「中枢对象」。它不是某个具体算法的封装，而是一个**状态容器 + 调度器**：把「当前在做什么模式」「训练到第几步」「网络长什么样」「相机在哪」「GUI 窗口句柄」「多块 GPU 的副本」……所有状态都收拢进一个类里。

这种写法在工程上叫「上帝对象（God Object）」，通常被视为反模式。但 instant-ngp 选择它是有理由的：

1. 四种基元共享大量逻辑——帧循环、文件加载、相机控制、快照存读、CUDA-GL 互操作——这些逻辑只愿写一份，自然落到一个公共类上。
2. 模式之间字段差异大，又需要频繁切换，把差异字段做成内嵌结构体（`m_nerf` 等）比继承体系更直接。
3. 这是一份研究代码，优先级是「快速验证算法」而非「架构纯净」。

理解了这一点，你就能接受 `testbed.h` 有 1294 行、`testbed.cu` 有 5672 行——它们都是这一个类的声明与实现。

#### 4.1.2 核心流程

`Testbed` 对象的生命周期可以画成下面这台状态机：

```text
构造 Testbed(mode)
   │  1. 选定 GUI 所在 GPU、建主设备
   │  2. 枚举辅助 GPU
   │  3. 填一份默认 m_network_config（loss/optimizer/encoding/network 四块）
   │  4. set_mode(mode)  ← 把模式专属状态和网络成员按 mode 初始化
   ▼
[运行中]  frame() 循环每帧调用 train() + render_frame()
   │  train() 按 m_testbed_mode 分发到 train_nerf / train_sdf / ...
   │  若 m_trainer 为空则先 reload_network_from_file() 现场建网络
   ▼
set_mode(new_mode)  ← 用户换模式时：清空旧状态 → 设新默认值
   │  （网络成员被置空，下次 train() 触发 reset_network 重建）
   ▼
析构 ~Testbed()  ← 释放临时文件、GPU 资源随 shared_ptr 自动释放
```

关键点：**网络不是在构造时建好的，而是「按需重建」**。构造只设默认配置和模式；真正的网络对象在第一次 `train()` 或显式 `reload_network_from_file()` 时才由 `reset_network()` 搭起来。这让「换模式」「换配置」都很便宜。

#### 4.1.3 源码精读

类从 `class Testbed {` 开始，构造函数有四个重载，形成一条委托链：

[include/neural-graphics-primitives/testbed.h:L73-L82](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L73-L82) —— 上面声明无参版本（默认 `ETestbedMode::None`），下面三个重载依次委托：带数据路径的先调 `load_training_data`，带配置路径/JSON 的再调 `reload_network_from_file` / `reload_network_from_json`。C++ 委托构造让「只给模式」「给模式+数据」「给模式+数据+网络」三种用法共用同一套初始化。

真正的构造实现里，最值得看的两段：

[src/testbed.cu:L4472-L4492](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4472-L4492) —— 先把当前 GPU 作为「主设备」`emplace_back(active_device, true)` 放进 `m_devices`，再遍历所有 GPU，把算力达标的其余 GPU 作为「辅助设备」加进去。注释明确写着 `// Multi-GPU is only supported in NeRF mode for now`，这就是后面 `set_mode` 里多 GPU 只对 NeRF 启用的根源。

[src/testbed.cu:L4494-L4523](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4494-L4523) —— 构造函数直接在代码里写死了一份默认 `m_network_config`，正好包含 `loss / optimizer / encoding / network` 四块（默认 HashGrid 编码 + FullyFusedMLP）。这份默认配置保证了即使你不指定任何 `.json`，程序也能建出一个可用的网络。最后调用 `set_mode(mode)` 把模式设进去。

#### 4.1.4 代码实践

**实践目标**：确认「网络是按需重建」这一设计，定位构造与重建的边界。

**操作步骤**：

1. 打开 [src/testbed.cu:L4414-L4528](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4414-L4528) 的构造函数，逐段标注它做了哪几件事（GPU 选择、设备枚举、默认配置、`set_mode`）。
2. 注意构造函数里**没有**出现 `create_loss` / `create_network` / `m_trainer = ...` 之类的调用——确认网络对象不是在这里建的。
3. 再看 [src/testbed.cu:L4575-L4580](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4575-L4580)：`train()` 开头判断 `if (!m_trainer)` 就调用 `reload_network_from_file()` 现场建网络。

**需要观察的现象**：构造函数的职责是「准备环境和默认配置」，而真正分配显存、建网络发生在第一次训练时。

**预期结果**：你能在构造函数里找到 `m_devices`、`m_network_config`、`set_mode`，但找不到 `m_trainer` 的赋值；反过来 `m_trainer` 的赋值出现在 `reset_network()`（下一模块会看）和 `train()` 的兜底分支里。

#### 4.1.5 小练习与答案

**练习 1**：构造函数里 `m_devices.emplace_back(active_device, true)` 的第二个参数 `true` 表示什么？为什么不把所有 GPU 都标成 `true`？

> **答案**：第二个参数是 `is_primary`，标记主设备。主设备负责承载 GUI/OpenGL 上下文和最终上屏；辅助设备只参与并行计算。把多块 GPU 都标成主设备会争抢 GL 上下文，且 `render_frame_main` 的多设备分工逻辑假设只有一块主设备。

**练习 2**：为什么构造函数要内置一份默认 `m_network_config`，而不是要求用户必传 `.json`？

> **答案**：让程序在「只给场景、不给网络配置」时也能跑起来——`reload_network_from_file()` 在路径为空时会沿用 `m_network_config`，构造时填的默认值正好兜底。这也方便 GUI 里临时切换配置而不至于崩在没有配置的状态。

---

### 4.2 ETestbedMode 与模式切换

#### 4.2.1 概念说明

`ETestbedMode` 是整个项目的「模式开关」枚举，定义在 `common.h`：

```cpp
enum class ETestbedMode : int {
    Nerf,
    Sdf,
    Image,
    Volume,
    None,
};
```

四个基元对应 `Nerf/Sdf/Image/Volume`，外加一个 `None`。`None` 是**哨兵值**，表示「还没决定模式」——刚构造出来的 `Testbed`、或者刚被 `set_mode` 清空之后就是这个状态。在 `None` 下既不能训练也不能渲染，必须先通过加载文件（`load_file` 自动判别模式）或显式 `set_mode` 切到某个具体模式。

注意 `ETestbedMode` 是 `enum class`（强类型枚举），不会隐式转成 `int`，必须写 `ETestbedMode::Nerf` 全名，避免和其它枚举撞名。

#### 4.2.2 核心流程

模式切换是一台小型状态机，`set_mode(mode)` 是唯一的转移函数：

```text
         ┌──────────────────────────────────────┐
         │  set_mode(new_mode)                  │
         │  if (new_mode == m_testbed_mode)     │  ← 同模式直接返回
         │      return;                         │
         │  1. 清空模式专属状态                  │  m_image/m_mesh/m_nerf/m_sdf/m_volume = {}
         │  2. 清空网络相关成员                  │  m_encoding/m_loss/m_network/... = {}
         │  3. 清空各设备 device.clear()         │
         │  4. 清空数据路径 m_data_path = {}     │
         │  5. m_testbed_mode = new_mode         │  ← 真正切换
         │  6. 按新模式设默认值                  │  多GPU/DLSS 等开关
         │  7. reset_camera()                    │
         └──────────────────────────────────────┘
```

要点：**先清后设**。`m_testbed_mode` 在第 5 步才被赋新值，前面所有清空用的都是旧语义；新模式的默认值（第 6 步）在新值生效后才设。

`m_testbed_mode` 一旦确定，就成了全程序的分发开关。三处分发都靠 `switch (m_testbed_mode)`：

- `load_training_data` → `load_nerf / load_mesh / load_image / load_volume`
- `train` → 先 `training_prep_*` 再 `train_nerf / train_sdf / train_image / train_volume`
- `render_frame_main` → `render_nerf / render_sdf / render_image / render_volume`

#### 4.2.3 源码精读

枚举本身在最不起眼的位置：

[include/neural-graphics-primitives/common.h:L149-L155](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/common.h#L149-L155) —— `ETestbedMode` 定义，注意 `None` 排在最后作为哨兵。

模式与字符串的互转在 `common_host.cu`，这个转换直接决定了配置文件路径：

[src/common_host.cu:L176-L185](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L176-L185) —— `to_string(ETestbedMode)` 返回**小写**的 `"nerf"/"sdf"/"image"/"volume"/"none"`。这与仓库里 `configs/nerf`、`configs/sdf`、`configs/image`、`configs/volume` 四个目录名**完全一致**，并非巧合——`find_network_config` 正是用它拼路径。

`set_mode` 的实现是本模块的核心：

[src/testbed.cu:L195-L252](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L195-L252) —— 完整的「先清后设」。第 201-205 行清空五个模式专属结构体（含 `m_mesh`），第 208-215 行把网络相关成员和 `m_envmap/m_distortion` 全部置空，第 219-221 行对每块 GPU 调 `device.clear()`，第 226 行才真正 `m_testbed_mode = mode`。

[src/testbed.cu:L229-L245](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L229-L245) —— 新模式的默认值：只有 `Nerf` 才在多 GPU 时启用 `m_use_aux_devices`（呼应构造函数里那条注释），也只有 `Nerf` 才允许启用 DLSS。这就是「多 GPU / DLSS 仅 NeRF 支持」在代码里的落地处。

分发开关的一个典型例子——`train()` 按模式派发训练：

[src/testbed.cu:L4634-L4638](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4634-L4638) —— `switch (m_testbed_mode)` 把控制流交给 `train_nerf / train_sdf / train_image / train_volume`，`default` 分支抛异常。`training_prep_*`（[L4606-L4609](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4606-L4609)）和 `load_training_data`（[L172-L176](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L172-L176)）是同样形态的 switch。

#### 4.2.4 代码实践

**实践目标**：验证「模式 → 配置目录」的映射，理解 `to_string` 与 `configs/` 目录的对应关系。

**操作步骤**：

1. 在仓库根目录执行 `ls configs/`，确认有 `image nerf sdf volume` 四个子目录。
2. 打开 [src/common_host.cu:L176-L185](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/common_host.cu#L176-L185)，核对 `to_string` 返回的字符串与目录名一致（全小写）。
3. 打开 [src/testbed.cu:L264-L269](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L264-L269) 的 `find_network_config`，看它如何用 `root_dir() / "configs" / to_string(m_testbed_mode) / network_config_path` 拼出配置路径。
4. 思考：若 `to_string(ETestbedMode::Nerf)` 返回的是 `"NeRF"` 而非 `"nerf"`，会发生什么？

**需要观察的现象**：模式枚举、字符串、磁盘目录三者形成一条干净的映射链。

**预期结果**：当 `m_testbed_mode == Nerf` 且 `m_network_config_path == "base.json"` 时，`find_network_config` 会解析成 `configs/nerf/base.json`。若字符串大小写不一致，该路径在区分大小写的文件系统上就找不到文件，会回退并打印 warning。

#### 4.2.5 小练习与答案

**练习 1**：`set_mode` 第一行 `if (mode == m_testbed_mode) return;` 有什么作用？去掉它会有什么后果？

> **答案**：幂等保护——重复设同一模式时直接返回，避免无谓地清空已训练好的状态和重建网络。去掉它，每次 GUI 里误触同一模式都会把 `m_nerf`、`m_trainer` 等全部清空，训练进度丢失。

**练习 2**：为什么 `m_testbed_mode = mode;` 这一行要放在清空操作（第 201-221 行）**之后**，而不是函数开头？

> **答案**：清空阶段如果引用了 `m_testbed_mode`（例如未来在清理时需要按旧模式做差异化释放），必须保持旧值；先把 `m_testbed_mode` 改成新值会破坏这一前提。当前的清空是统一置空，不依赖旧值，但「先清旧、再设新」的顺序本身是安全且符合状态机直觉的。

---

### 4.3 模式专属状态：m_nerf / m_sdf / m_image / m_volume

#### 4.3.1 概念说明

四种基元各自需要完全不同的训练数据、渲染参数和中间缓冲。Testbed 的做法是：把每种基元的全部状态打包成一个**内嵌结构体**，作为 `Testbed` 的成员变量。这样四种基元的字段不会互相污染，切换模式时整个结构体一扔了之。

四个内嵌结构体分别是：

| 成员 | 类型 | 承载的状态 | 典型字段 |
| --- | --- | --- | --- |
| `m_nerf` | `struct Nerf` | NeRF 数据集、密度网格、相机优化、误差图 | `training.dataset`、`density_grid`、`density_grid_bitfield` |
| `m_sdf` | `struct Sdf` | 网格三角面、BVH/八叉树、SDF 训练样本 | `triangle_bvh`、`triangle_octree`、`distance_scale` |
| `m_image` | `struct Image` | 待拟合的图像数据与像素采样目标 | `data`、`resolution`、`random_mode` |
| `m_volume` | `struct Volume` | NanoVDB 稀疏体素网格与体积渲染参数 | `nanovdb_grid`、`global_majorant`、`world2index_scale` |

注意每个结构体里几乎都还内嵌了一个 `struct Training { ... } training = {};`，专门放「训练循环用得到的临时缓冲和超参」。这种「外层放数据/参数、内层 `Training` 放训练态」的二分贯穿四个结构体。

#### 4.3.2 核心流程

四个结构体在 `set_mode` 里被一并值初始化清空：

```text
m_image = {};  m_mesh = {};  m_nerf = {};  m_sdf = {};  m_volume = {};
```

`{}` 会触发每个结构体成员的默认构造：`GPUMemory<T>` 释放旧显存并置空、`shared_ptr` 引用计数减一（归零则释放对象，比如 `triangle_bvh`、`triangle_octree`）、`std::vector` 清空、数值字段回到声明时的默认值（如 `m_volume.albedo = 0.95f`）。

之所以要「清空」而不是「保留」，是因为：**旧模式的状态对新模式毫无意义，甚至有害**。例如从 NeRF 切到 SDF 时，`m_nerf.density_grid`（一个 NERF_GRIDSIZE³ 的密度场）对 SDF 毫无用处却占着显存；`m_sdf.triangle_bvh` 又必须从新加载的网格重建。一刀切清空是最稳妥的做法。

#### 4.3.3 源码精读

四个结构体的声明集中在 `testbed.h` 中段。先看 NeRF（体量最大）：

[include/neural-graphics-primitives/testbed.h:L741-L897](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L741-L897) —— `struct Nerf`，内嵌 `struct Training`（L742-L858）放相机优化器、误差图、`counters_rgb`、各种 `optimize_*` 开关；外层放密度网格与渲染参数。最关键的两类字段：`density_grid`（[L860](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L860)，EMA 平滑的密度场）和 `density_grid_bitfield`（[L861](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L861)，压缩成位域用于渲染时跳过空区域）。`max_cascade`（[L866](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L866)）控制大场景的多级联。

SDF 结构体：

[include/neural-graphics-primitives/testbed.h:L899-L945](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L899-L945) —— `struct Sdf`。最关键的两类字段：`triangle_bvh`（[L917](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L917)，`shared_ptr<TriangleBvh>`，用于求网格真值距离）和 `triangle_octree`（[L922](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L922)，`shared_ptr<TriangleOctree>`，支持 Takikawa 编码）。还有 `mesh_sdf_mode`（[L909](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L909)）决定真值距离的计算方式。内嵌 `Training`（[L932-L944](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L932-L944)）持有在线生成的 `positions/distances` 训练样本。

图像结构体：

[include/neural-graphics-primitives/testbed.h:L952-L971](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L952-L971) —— `struct Image`。最关键的两类字段：`data`（[L953](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L953)，`GPUMemory<char>` 存像素数据）和 `resolution`（[L956](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L956)）。`random_mode`（[L970](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L970)）控制像素采样策略。

体素结构体：

[include/neural-graphics-primitives/testbed.h:L979-L999](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L979-L999) —— `struct Volume`。最关键的两类字段：`nanovdb_grid`（[L983](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L983)，`GPUMemory<char>` 存 NanoVDB 稀疏体素数据）和 `global_majorant`（[L985](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L985)，体积步进时单步最大的衰减系数上界）。`world2index_scale`（[L987](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L987)）是世界坐标到体素索引的缩放。

清空这四个结构体的代码在 `set_mode`：

[src/testbed.cu:L200-L216](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L200-L216) —— 第 201-205 行清空五个模式结构体（含 `m_mesh`），第 208-215 行清空网络相关成员（下一模块详述）。

#### 4.3.4 代码实践

**实践目标**：在 `testbed.h` 中定位四个内嵌结构体，记录各自最关键的 2 个字段，并解释 `set_mode` 为什么要清空它们。

**操作步骤**：

1. 在 `testbed.h` 里分别找到 `} m_nerf;`（[L897](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L897)）、`} m_sdf;`（[L945](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L945)）、`} m_image;`（[L971](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L971)）、`} m_volume;`（[L999](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L999)），往上回溯确认结构体边界。
2. 为每个结构体挑出你认为「最关键」的 2 个字段，记下它的行号与含义。参考答案见下。
3. 回到 [src/testbed.cu:L200-L205](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L200-L205)，确认这四个结构体在 `set_mode` 里被 `= {}` 整体清空。

**需要观察的现象**：四个结构体字段类型差异极大（NeRF 是密度网格、SDF 是 BVH、Image 是像素缓冲、Volume 是 NanoVDB），但它们都以同样的 `= {}` 方式被统一清空。

**预期结果**（参考答案，可选其它等价字段）：

| 结构体 | 关键字段 1 | 关键字段 2 |
| --- | --- | --- |
| `m_nerf` | `density_grid`（L860，EMA 密度场） | `density_grid_bitfield`（L861，位域用于跳过空区域） |
| `m_sdf` | `triangle_bvh`（L917，网格加速结构） | `triangle_octree`（L922，Takikawa 编码用八叉树） |
| `m_image` | `data`（L953，像素数据） | `resolution`（L956，图像分辨率） |
| `m_volume` | `nanovdb_grid`（L983，NanoVDB 体素） | `global_majorant`（L985，体积步进上界） |

**为什么 `set_mode` 要清空它们**：① 旧模式的字段对新模式无意义且占显存（如 NeRF 的密度网格对 SDF 无用）；② 新模式必须从新加载的数据重新构建自己的加速结构和缓冲（如 SDF 的 BVH 要按新网格重建），保留旧数据反而会误导渲染；③ `= {}` 触发 `shared_ptr`/`GPUMemory` 析构，自动释放旧 GPU 资源，避免显存泄漏。统一清空比逐字段判断「能否复用」简单可靠得多。

#### 4.3.5 小练习与答案

**练习 1**：`m_sdf.triangle_bvh` 是 `std::shared_ptr<TriangleBvh>`。`set_mode` 里写 `m_sdf = {};` 之后，原来的 BVH 对象会立即被销毁吗？

> **答案**：不一定立即销毁。`shared_ptr` 是引用计数——`m_sdf = {}` 把 `m_sdf.triangle_bvh` 这一个引用置空，引用计数减一；只有当计数归零（没有其它 `shared_ptr` 指向同一 BVH）时才销毁。在本项目里 BVH 通常只被 `m_sdf` 独占，所以一般会立即释放；但机制上取决于是否还有其它持有者。

**练习 2**：为什么每个模式结构体里都再内嵌一个 `struct Training { ... } training = {};`，而不是把训练字段直接摊在外层？

> **答案**：把「静态数据/参数」与「训练循环的临时态」分层，便于阅读和重置。训练态（采样位置、梯度缓冲、计数器、`optimize_*` 开关）往往需要在 `reset_network` 或新一轮训练开始时单独重置，而数据/参数（分辨率、网格、距离缩放）应保持不变。内嵌 `Training` 让这两类状态在代码上一目了然。

---

### 4.4 顶层网络成员：loss / optimizer / encoding / network / trainer

#### 4.4.1 概念说明

除了四种基元各自的状态，Testbed 还持有五个**跨模式共享**的顶层网络成员。它们都来自 tiny-cuda-nn，用 `std::shared_ptr` 持有：

| 成员 | 类型 | 职责 |
| --- | --- | --- |
| `m_loss` | `shared_ptr<Loss<...>>` | 损失函数（L2/L1/Smape/...），衡量预测与真值的差距。 |
| `m_optimizer` | `shared_ptr<Optimizer<...>>` | 优化器（Adam 等，可嵌套 EMA/学习率衰减），按梯度更新参数。 |
| `m_encoding` | `shared_ptr<Encoding<...>>` | 输入编码（HashGrid/OneBlob/Frequency/...），把坐标映射到高维特征。 |
| `m_network` | `shared_ptr<Network<...>>` | MLP 主体，把编码后的特征映射到输出。 |
| `m_trainer` | `shared_ptr<Trainer<...>>` | 训练器，把上面四者串起来，负责前向/反向/混合精度/参数快照。 |

另外还有 `m_nerf_network`（`shared_ptr<NerfNetwork<...>>`），它是 NeRF 模式专用的「双头网络」，把位置编码、密度 MLP、方向编码、颜色 MLP 打包成一个对象；在 NeRF 模式下 `m_network` 和 `m_nerf_network` 指向同一对象。

这五个成员与四种基元状态的关系是：**模式状态决定「拿什么数据训练」，网络成员决定「用什么模型训练」**。换模式会清空两者；换配置（`reload_network_from_file`）只重建网络成员，不动模式状态。

#### 4.4.2 核心流程

五个成员的构造集中在 `reset_network()`，它由 `reload_network_from_file/json` 触发（或 `train()` 检测到 `m_trainer` 为空时兜底触发）。流程：

```text
reset_network()
  │  1. 重置训练计数、密度网格、相机外参等训练态
  │  2. 取出 m_network_config 的四块：encoding/loss/optimizer/network
  │  3. 计算 network_dims()（按模式得到 n_input/n_output/n_pos）
  │  4. 若是 HashGrid 等网格编码：自动推导 n_levels、base_resolution、per_level_scale
  │  5. m_loss     = create_loss(loss_config)            ← 工厂
  │     m_optimizer = create_optimizer(optimizer_config)  ← 工厂
  │  6. 分模式建网络：
  │     - Nerf：每设备建 NerfNetwork（双头）；m_network = m_nerf_network；m_encoding = nerf.pos_encoding()
  │     - 其它：按对齐要求(16/8)建 NetworkWithInputEncoding；m_encoding = create_encoding(...)
  │  7. m_trainer = make_shared<Trainer>(m_network, m_optimizer, m_loss, seed)
  │  8. 顺带建 envmap / distortion 两个可选子模型
  ▼
训练就绪：m_trainer 可被 train_* 调用
```

`network_dims()` 本身也是一个按 `m_testbed_mode` 的 switch：

[src/testbed.cu:L4150-L4158](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4150-L4158) —— `Nerf→network_dims_nerf()`、`Sdf→network_dims_sdf()`、`Image→network_dims_image()`、`Volume→network_dims_volume()`，返回 `{n_input, n_output, n_pos}`。这个维度信息驱动编码与网络的形状。

关于混合精度：Testbed 用 `LOSS_SCALE()` 把损失放大，避免半精度梯度下溢：

[include/neural-graphics-primitives/testbed.h:L307-L311](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L307-L311) —— 注释解释：混合精度下小梯度可能下溢为 0，所以把 loss（进而梯度）放大一个因子，优化器里再除掉。`LOSS_SCALE()` 来自 `default_loss_scale<network_precision_t>()`。

#### 4.4.3 源码精读

五个成员的声明紧挨在一起：

[include/neural-graphics-primitives/testbed.h:L1236-L1240](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1236-L1240) —— `m_loss / m_optimizer / m_encoding / m_network / m_trainer` 五个 `shared_ptr`，注意声明顺序大致就是「依赖顺序」：loss 和 optimizer 不依赖别人，encoding 也不依赖，network 由 encoding 喂入，trainer 把前三者和 network 串起来。

[include/neural-graphics-primitives/testbed.h:L1290-L1290](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1290) —— `m_nerf_network` 单独声明在末尾，NeRF 模式下与 `m_network` 指向同一对象。

`reset_network()` 里五个成员的构造点：

[src/testbed.cu:L4262-L4263](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4262-L4263) —— `create_loss` 和 `create_optimizer` 是 tiny-cuda-nn 的工厂函数，传入 JSON 配置返回对应对象。`m_loss.reset(...)` / `m_optimizer.reset(...)` 把 `shared_ptr` 重置为新对象（释放旧的）。

[src/testbed.cu:L4266-L4327](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4266-L4327) —— **NeRF 分支**：为每块 GPU 建一个 `NerfNetwork`（双头），再令 `m_network = m_nerf_network = primary_device().nerf_network()`（[L4296](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4296)），并 `m_encoding = m_nerf_network->pos_encoding()`（[L4298](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4298)）。注意 NeRF 的损失由专用路径处理，所以这里把 `loss_config["otype"]` 强制改成 `"L2"` 当占位（见 [L4208-L4215](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4208-L4215)）。

[src/testbed.cu:L4328-L4374](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4328-L4374) —— **其它三种模式分支**：先按 `otype` 决定对齐量 `alignment`（FullyFusedMLP/MegakernelMLP 要 16，其余 8，见 [L4329-L4333](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4329-L4333)）；再用 `create_encoding` 建编码（[L4354](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4354)）；最后为每设备建 `NetworkWithInputEncoding`（[L4363](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4363)），把编码和 MLP 组合，令 `m_network = primary_device().network()`（[L4366](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4366)）。

[src/testbed.cu:L4383-L4383](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4383-L4383) —— 最后用 `make_shared<Trainer>(m_network, m_optimizer, m_loss, m_seed)` 把网络、优化器、损失三者组装成 `m_trainer`。从此 `train_*` 就能调用 `m_trainer->training_step(...)` 跑一步训练。

`set_mode` 清空这些成员的代码：

[src/testbed.cu:L207-L216](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L207-L216) —— 把 `m_encoding/m_loss/m_network/m_nerf_network/m_optimizer/m_trainer/m_envmap/m_distortion` 全部 `= {}` 置空，并把 `m_training_data_available` 设为 `false`。这就是「换模式后必须重建网络」的根因——清空后 `m_trainer` 为空，下一次 `train()` 会兜底调用 `reload_network_from_file()` → `reset_network()` 重建。

#### 4.4.4 代码实践

**实践目标**：在 `reset_network()` 中追踪五个成员的构造顺序，验证「工厂函数 + shared_ptr」的拼装方式。

**操作步骤**：

1. 打开 [src/testbed.cu:L4262-L4263](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4262-L4263)，确认 `m_loss`、`m_optimizer` 由 `create_loss` / `create_optimizer` 工厂构造。
2. 顺着 NeRF 分支 [L4266-L4327](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4266-L4327) 看 `m_network`、`m_nerf_network`、`m_encoding` 如何从 `NerfNetwork` 对象上「取」出来（`pos_encoding()`）。
3. 看 `m_trainer` 在 [L4383](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4383) 如何把前三者打包。
4. 对照 [src/testbed.cu:L207-L216](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L207-L216) 的清空，确认 `set_mode` 后这五个成员都为空，需要 `reset_network` 重建。

**需要观察的现象**：构造顺序是 `loss/optimizer → encoding/network → trainer`，正好是依赖链的顺序；`m_trainer` 最后才建，因为它依赖前四者。

**预期结果**：你能在 `reset_network` 里依次找到 `m_loss`、`m_optimizer`、`m_encoding`、`m_network`（或 `m_nerf_network`）、`m_trainer` 的赋值点，且 `m_trainer` 的构造明显晚于其它四个。若跳过 `reset_network` 直接调用 `train()`，会命中 [L4575-L4580](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4575-L4580) 的兜底分支被迫重建。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `m_loss` 和 `m_optimizer` 用 `create_loss` / `create_optimizer` 工厂构造，而不是直接 `new`？

> **答案**：因为损失和优化器都是多态的——JSON 里 `otype` 可以是 `L2/L1/Smape/Huber/...` 或 `Adam/SGD/...`，甚至优化器可嵌套（EMA 包学习率衰减包 Adam）。工厂根据配置返回不同的派生类对象，调用方只持有基类 `shared_ptr<Loss>` / `shared_ptr<Optimizer>`，不必关心具体类型。这比在应用层写一堆 `if/else` 干净。

**练习 2**：NeRF 模式下 `m_network` 和 `m_nerf_network` 是什么关系？为什么需要两个名字？

> **答案**：它们指向**同一个** `NerfNetwork` 对象（`m_network = m_nerf_network = primary_device().nerf_network()`）。`m_network` 是基类 `shared_ptr<Network<...>>`，供通用训练/渲染路径（如 `m_trainer`、JIT 融合）使用；`m_nerf_network` 是派生类 `shared_ptr<NerfNetwork<...>>`，供需要访问双头结构（密度头、颜色头、`pos_encoding()`、`density_forward()`）的 NeRF 专用路径使用。两个名字是为了在「通用接口」和「NeRF 专有接口」之间不必反复 `dynamic_cast`。

**练习 3**：`set_mode` 把 `m_trainer` 置空后，如果不重新 `reload_network_from_file`，下一次 `train()` 会怎样？

> **答案**：`train()` 开头检测到 `if (!m_trainer)`（[L4575](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4575)）会自动调用 `reload_network_from_file()` 兜底重建；若重建后仍为空（例如配置缺失），才抛 `Unable to create a neural network trainer.` 异常。所以普通使用中你不必手动重建。

## 5. 综合实践

把本讲四个模块串起来，完成一次「**从构造到换模式的完整追踪**」：

1. **构造阶段**：阅读 [src/testbed.cu:L4414-L4528](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4414-L4528) 的构造函数，画出它做了哪 4 件大事（GPU 选择 → 设备枚举 → 默认配置 → `set_mode`）。确认此时 `m_trainer` 仍为空。
2. **模式与状态**：打开 [src/testbed.cu:L195-L252](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L195-L252) 的 `set_mode`，列出它清空的「模式专属结构体」和「网络成员」两组清单。再到 `testbed.h` 找到 `m_nerf / m_sdf / m_image / m_volume`（[L897/L945/L971/L999](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L897)），为每个记下 2 个关键字段。
3. **网络重建**：假设用户随后加载了 `configs/nerf/base.json`，追踪 `reload_network_from_file` → `reset_network`（[L4160](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4160)），找到 `m_loss / m_optimizer / m_encoding / m_network / m_trainer` 五个赋值点，标注它们的先后顺序。
4. **分发验证**：确认 `m_testbed_mode == Nerf` 后，`train()` 的 switch（[L4634-L4638](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4634-L4638)）会走到 `train_nerf`。

**交付物**：一张包含「构造 → set_mode → reset_network → train 分发」四阶段、每阶段标注关键行号与涉及成员的流程图（手绘或文字伪代码皆可）。

> 本实践为源码阅读型，不要求运行；若要运行验证，可编译后用 `./instant-ngp data/nerf/fox` 观察日志里 `MultiLevelEncoding:...` 和 `Density model:...` 的输出（来自 `reset_network` 的 `tlog::info`），即对应第 3 步的网络重建。

## 6. 本讲小结

- `Testbed` 是一个承载全部状态的「上帝对象」：GPU 设备、模式、训练态、网络、相机、GUI 全收拢于一个类，共享逻辑只写一份。
- `ETestbedMode` 有 `Nerf/Sdf/Image/Volume` 四个基元加 `None` 哨兵；`to_string` 返回的小写字符串与 `configs/` 四个子目录一一对应，驱动配置路径解析。
- `set_mode()` 遵循「先清后设」：先清空 `m_nerf/m_sdf/m_image/m_volume` 等模式专属状态和五个网络成员，再设新 `m_testbed_mode` 和模式默认值（多 GPU、DLSS 仅 NeRF 启用）。
- 四个模式结构体字段差异极大（密度网格 / BVH+八叉树 / 像素缓冲 / NanoVDB），但都用统一的 `= {}` 值初始化清空，靠 `shared_ptr`/`GPUMemory` 析构自动释放显存。
- 五个顶层网络成员 `m_loss/m_optimizer/m_encoding/m_network/m_trainer` 由 `reset_network()` 用 tiny-cuda-nn 工厂按依赖顺序构造，NeRF 走 `NerfNetwork` 双头、其它走 `NetworkWithInputEncoding`。
- 网络是「按需重建」的：构造与 `set_mode` 只置空，真正建网发生在 `reset_network()`，或 `train()` 检测到 `m_trainer` 为空时的兜底分支。

## 7. 下一步学习建议

本讲只看了「类的骨架与模式分发」，还没进入一帧内部到底发生了什么。建议按顺序继续：

- **`u2-l2` 主帧循环**：追踪 `frame() → train_and_render() → train() → render_frame()` 的一帧时序，理解训练与渲染如何交替、何时跳过渲染。本讲的 `train()` 分发正是其中一环。
- **`u2-l3` 文件加载与模式自动识别**：看 `load_file` / `mode_from_scene` 如何根据文件后缀自动决定本讲的 `ETestbedMode`，把「拖一个文件进来」和「设一个模式」连起来。
- **`u2-l4` 网络配置体系**：深入 `configs/*.json` 的 `encoding/network/optimizer/loss` 四大块与 `parent` 继承，理解本讲 `reset_network()` 读的那份 `m_network_config` 是怎么来的。
- 想直接看网络创新可跳到 **`u3-l2` 多分辨率哈希编码**，但建议先补 `u2-l4` 的配置基础。
