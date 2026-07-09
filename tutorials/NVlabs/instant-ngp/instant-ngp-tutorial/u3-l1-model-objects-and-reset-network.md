# tiny-cuda-nn 模型对象与 reset_network

## 1. 本讲目标

本讲是「神经网络与多分辨率哈希编码」单元的第一篇。上一单元（u2-l4）我们讲到：`reload_network_from_file` 最终把一份 JSON 配置填进了 `Testbed::m_network_config`，但「配置如何变成一个能训练、能前向、能反向的真实网络」这件事还没发生。

学完本讲，你应当能够：

- 说清 `reset_network()` 为什么是「网络重建的总装车间」，它在哪些时机被调用。
- 掌握 Testbed 持有的五个 `shared_ptr` 模型对象 `m_loss / m_optimizer / m_encoding / m_network / m_trainer` 各自的职责与依赖顺序。
- 认识 `create_loss / create_optimizer / create_encoding` 这些来自 tiny-cuda-nn 的工厂函数，以及网络为何通过 `NerfNetwork` 或 `NetworkWithInputEncoding` 两个「包装器」来构造。
- 理解混合精度训练下 `LOSS_SCALE` 为什么要把损失（从而梯度）整体放大一个常数倍。

> 边界提示：`per_level_scale` 等哈希编码参数的数学推导是下一讲（u3-l2）的主题；本讲只把它们当作 `reset_network` 流程中的一个「自动参数填充」步骤一笔带过。

## 2. 前置知识

阅读本讲前，建议你已经具备以下概念（来自 u1、u2 单元）：

- **Testbed 是「上帝对象」**：它同时持有 GPU、模式、训练态、网络、相机、GUI 等几乎所有状态（见 u2-l1）。
- **`ETestbedMode` 四种模式**：`Nerf / Sdf / Image / Volume`，外加哨兵 `None`。`m_testbed_mode` 是几乎所有分发逻辑的开关。
- **网络配置 JSON**：`m_network_config` 里包含 `encoding / network / optimizer / loss` 四大块；NeRF 还多出 `dir_encoding / rgb_network / distortion_map / envmap`（见 u2-l4）。
- **「按需重建」结论**：`set_mode` 切换模式时只把网络成员**置空**，并不真正建网；真正建网发生在 `reset_network()`（见 u2-l1、u2-l2）。
- **C++ 智能指针 `shared_ptr`**：多个对象共享同一块显存/网络所有权，引用计数归零时自动释放。

另外两个术语需要先解释：

- **混合精度训练（mixed precision）**：网络的中间计算用低精度（半精度 `half`，即 fp16）以节省显存、提升速度，而参数主副本和梯度累加用单精度（`float`）以保留精度。instant-ngp 用类型别名 `network_precision_t` 表示网络计算精度（来自 tiny-cuda-nn，经由 `common.h` 的 `using namespace tcnn;` 引入）。
- **工厂函数（factory function）**：给定一段 JSON，返回一个构造好的、派生类未知的对象指针。调用方只需面向基类（`Loss`/`Optimizer`/`Encoding`/`Network`）编程，由工厂根据 JSON 里的 `otype` 字段决定实例化哪个具体子类。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/neural-graphics-primitives/testbed.h](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h) | 声明五个网络成员、`LOSS_SCALE()`、`NetworkDims`、`reset_network` 原型 |
| [src/testbed.cu](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu) | `reset_network()`、`network_dims()`、`set_mode()` 的实现，以及 `reload_network_from_file`、`train()` 中的调用点 |
| [configs/nerf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json) | NeRF 默认配置：可看到 `Ema → ExponentialDecay → Adam` 嵌套的 optimizer |
| [configs/sdf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json) | SDF 默认配置：与 NeRF 对照 loss/optimizer 的差异 |

> 说明：`create_loss / create_optimizer / create_encoding`、类型 `network_precision_t`、函数 `default_loss_scale` 都定义在外部依赖 **tiny-cuda-nn** 中。本仓库的 submodule 在本讲解环境中未检出，因此本讲对这些符号只描述其在 tiny-cuda-nn 中的角色与用法，不引用 tiny-cuda-nn 内部的行号（如需核对，请在本地 `git submodule update --init --recursive` 后查看 `dependencies/tiny-cuda-nn/`）。

---

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：

1. **五大模型对象**：Testbed 持有的五个 `shared_ptr`、它们的职责、模板参数与 `LOSS_SCALE`。
2. **reset_network 流程**：从 JSON 取配置 → 按模式分发 → 填充自动参数 → 装配 Trainer。
3. **工厂函数与对象构造**：`create_*` 如何把 JSON 变成对象，`NerfNetwork` 与 `NetworkWithInputEncoding` 的差异。

### 4.1 五大模型对象

#### 4.1.1 概念说明

一个小型神经网络要能训练，至少需要四类零件 + 一个「总指挥」：

| 对象 | 类型 | 解决什么问题 |
| --- | --- | --- |
| `m_encoding` | `Encoding<network_precision_t>` | 把输入坐标（如 3D 位置）映射成高维特征向量（哈希网格、频率、OneBlob 等） |
| `m_network` | `Network<float, network_precision_t>` | MLP 本体：吃编码特征，吐预测值（密度、颜色、距离…） |
| `m_loss` | `Loss<network_precision_t>` | 衡量预测与真值的差距（L2、Huber、MAPE 等） |
| `m_optimizer` | `Optimizer<network_precision_t>` | 用梯度更新参数（Adam，常外层套 EMA、ExponentialDecay） |
| `m_trainer` | `Trainer<float, network_precision_t, network_precision_t>` | **总指挥**：编排一次「前向 → 算损失 → 反向 → 更新」的完整训练步骤 |

这五者不是平级的：`m_trainer` 持有另外几个的引用，负责把它们串起来。其余四个之间也有依赖——`m_network` 内部可能已经把 `m_encoding` 包了进去（见 4.3）。

#### 4.1.2 核心流程

```
       m_encoding  ──┐
                     ├──> m_network(MLP)  ──预测──┐
                                                  │
       m_loss  <──差距──  真值  <─────────────────┘
         │
         └──> m_trainer  ──反向求梯度──> m_optimizer ──更新参数──> m_network
```

`m_trainer` 是这五个里**最后**被构造的，因为它要把前三者握在手里。`reset_network()` 的构造顺序严格遵循这个依赖：先建 `m_loss`、`m_optimizer`，再建 `m_encoding`、`m_network`，最后才 `new Trainer(...)`。

#### 4.1.3 源码精读

五个成员声明在 `testbed.h` 连续五行里：

[include/neural-graphics-primitives/testbed.h:1236-1240](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1236-L1240) —— 连续声明 `m_loss / m_optimizer / m_encoding / m_network / m_trainer` 五个 `shared_ptr`。注意 `m_network` 的模板是 `Network<float, network_precision_t>`（输入用单精度、计算用网络精度），而 `m_trainer` 是三参数模板 `Trainer<float, network_precision_t, network_precision_t>`。

`LOSS_SCALE` 紧跟在注释里说明，定义在同一文件：

[include/neural-graphics-primitives/testbed.h:307-311](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L307-L311) —— 注释解释了为什么需要它：混合精度下，很小的损失值会让梯度在 fp16 计算中**下溢为 0**（round to zero），于是把损失整体放大 `LOSS_SCALE` 倍（梯度等比例放大），到优化器里再除掉。

`LOSS_SCALE()` 的值委托给 tiny-cuda-nn 的 `default_loss_scale<network_precision_t>()`，是编译期常量。对于常见的半精度（`half`）取值在 tiny-cuda-nn 中给出（具体数值待本地核对 `dependencies/tiny-cuda-nn/`）。

`NetworkDims` 是用来描述「这个模式需要几维输入、几维输出、几维位置」的小结构：

[include/neural-graphics-primitives/testbed.h:313-317](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L313-L317) —— `n_input / n_output / n_pos` 三个字段，由 `network_dims()` 按当前模式返回（见 4.2.3）。

#### 4.1.4 代码实践

1. **实践目标**：弄清五个成员的类型签名差异，理解为什么它们用不同模板参数。
2. **操作步骤**：打开 `include/neural-graphics-primitives/testbed.h`，定位到 1236–1240 行；再翻到 307–311 行读 `LOSS_SCALE` 的注释。
3. **需要观察的现象**：注意 `m_loss`/`m_optimizer`/`m_encoding` 是单参数模板（只有计算精度），而 `m_network`/`m_trainer` 是多参数模板。
4. **预期结果**：你能用一句话说出「为什么 `m_network` 要用 `Network<float, network_precision_t>` 而不是单参数」。参考答案见下方练习。
5. **待本地验证**：若想确认 `LOSS_SCALE` 在你这台机器上的实际数值，可在 tiny-cuda-nn 检出后查看 `default_loss_scale` 模板特化。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `m_loss`/`m_optimizer` 是 `Loss<network_precision_t>` 单参数模板，而 `m_network` 是 `Network<float, network_precision_t>` 双参数？

> **参考答案**：tiny-cuda-nn 里 `Network<InputT, ComputeT>` 把「输入数据类型」与「内部计算类型」分开——输入用 `float`（精确），内部激活、权重前向用 `network_precision_t`（半精度，快）。而 `Loss`/`Optimizer` 只关心计算精度，不接收外部输入张量类型，所以是单参数。

**练习 2**：`m_trainer` 为何放在最后才构造？

> **参考答案**：因为 `Trainer` 的构造函数要接收 `m_network`、`m_optimizer`、`m_loss` 和 `m_seed` 作为参数，依赖前三个对象先建好。它的职责就是协调这三者完成一次训练步。

---

### 4.2 reset_network 流程

#### 4.2.1 概念说明

`reset_network(bool clear_density_grid = true)` 是网络的「总装车间」。每当配置变化、模式切换、加载快照后，都需要它把一份 `m_network_config`（JSON）变成一套全新的、可训练的模型对象。它一次性做完三件事：

1. **重置训练临时状态**（密度网格、计数器、相机外参优化器、损失曲线…）。
2. **从 JSON 取出四大块**，按需**自动填充**网格编码的派生参数（如 `per_level_scale`）。
3. **按模式分发**构造网络对象，最后用它们 `new` 出 `m_trainer`。

关键认知：`reset_network` 会**从头训练**——它把 `m_training_step` 归零、重置计时器。这也是为什么加载 `.ingp` 快照时要么不调它（保留权重），要么调完后再反序列化权重（见 4.2.3 的调用点）。

#### 4.2.2 核心流程

`reset_network` 的执行骨架（伪代码）：

```
reset_network(clear_density_grid):
    1. 重置训练计数器 / 密度网格 / 相机外参优化器 / 损失曲线
    2. config = m_network_config                      # 复制一份，下面会就地改
       encoding_config / loss_config / optimizer_config / network_config
       = config["encoding"/"loss"/"optimizer"/"network"]
    3. dims = network_dims()                          # 按模式拿 n_input/n_output/n_pos
    4. 若是 grid/permuto 编码：自动推导 n_levels / base_resolution / per_level_scale
    5. m_loss     = create_loss(loss_config)          # 工厂
       m_optimizer = create_optimizer(optimizer_config)
    6. if Nerf:
           每块 GPU 各建一个 NerfNetwork(...)         # 双头：密度头 + 颜色头
           m_encoding = nerf_network->pos_encoding()
           （顺便建 distortion_map 副模型）
       else:
           alignment = (FullyFusedMLP/MegakernelMLP) ? 16 : 8
           m_encoding = create_encoding(dims.n_input, encoding_config)
           每块 GPU 各建一个 NetworkWithInputEncoding(m_encoding, ...)
    7. set_jit_fusion(m_jit_fusion)                   # 给每个设备开/关 JIT 融合
    8. m_trainer = new Trainer(m_network, m_optimizer, m_loss, m_seed)
    9. 设置 RTC 缓存目录；m_training_step = 0；记录开始时间
   10. （NeRF 专属）建 envmap 副模型
   11. set_all_devices_dirty()
```

注意步骤 6 的「每块 GPU 各建一个」——多 GPU 时每块卡都持有一份网络副本（多 GPU 仅 NeRF 启用，详见 u8-l1）。

#### 4.2.3 源码精读

函数签名与开头重置训练临时状态：

[src/testbed.cu:4160-4189](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4160-L4189) —— `m_sdf.iou_decay=0`、重置随机数发生器 `m_rng`、把渲染分辨率设回低值 `m_render_ms.set(10000)` 再逐步爬升、`reset_accumulation()`、清零 NeRF 训练计数器、`reset_camera_extrinsics()`，以及可选地清空 `density_grid` 与 `density_grid_bitfield`。

从 JSON 取出四大块配置（这是配置消费的真正起点）：

[src/testbed.cu:4192-4197](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4192-L4197) —— 先把 `m_network_config` 整体复制到局部 `config`（因为下面要就地写入自动参数），再取出四个子对象的引用。

`network_dims()` 按模式给出输入/输出维度：

[src/testbed.cu:4150-4158](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4150-L4158) —— `switch(m_testbed_mode)` 分发到 `network_dims_nerf/sdf/image/volume`。这是「模式分发」在本函数里的第一次出现。

自动填充网格编码派生参数（哈希编码专属，公式细节留给 u3-l2）：

[src/testbed.cu:4219-4259](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4219-L4259) —— 当 `encoding_otype` 含 `grid` 或 `permuto` 时，读取 `n_features_per_level / n_levels / log2_hashmap_size / base_resolution`，并在 `per_level_scale <= 0` 时自动推导它（依赖 `desired_resolution`，Image/Volume 模式会改写这个值），最后用 `tlog::info` 打印一行 `MultiLevelEncoding: ...`。

模式分发构造网络（NeRF 分支）：

[src/testbed.cu:4266-4327](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4266-L4327) —— NeRF 模式：先为每张图准备相机位姿/曝光/焦距的 Adam 优化器、`reset_extra_dims`，然后遍历 `m_devices` 用 `std::make_shared<NerfNetwork<...>>(...)` 建网（双头：位置编码→密度 MLP，方向编码→颜色 MLP）。`m_encoding` 直接取 `nerf_network->pos_encoding()`，避免重复构造。这里还顺带建了 `distortion_map`（畸变图）副模型。

模式分发构造网络（其他三种模式分支）：

[src/testbed.cu:4328-4374](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4328-L4374) —— 非 NeRF：先据 `network.otype` 判断对齐 `alignment`（`FullyFusedMLP`/`MegakernelMLP` 取 16，其余取 8）；若编码是 `Takikawa` 则建八叉树编码，否则 `create_encoding(...)`；最后遍历 `m_devices` 用 `std::make_shared<NetworkWithInputEncoding<...>>(m_encoding, dims.n_output, network_config)` 把「编码 + MLP」打包成一个对象。`m_network` 取主设备那份。

总指挥 Trainer 的装配（依赖前三者）：

[src/testbed.cu:4376-4389](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4376-L4389) —— `set_jit_fusion(m_jit_fusion)` 给每个设备开/关 JIT 融合；`m_trainer = std::make_shared<Trainer<float, network_precision_t, network_precision_t>>(m_network, m_optimizer, m_loss, m_seed)`；设置 RTC 缓存目录 `rtc/cache`；`m_training_step = 0` 并记录开始时刻——这标志着「从头训练」。

**调用时机**——理解 `reset_network` 必须知道它何时被调用：

- [src/testbed.cu:350](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L350) —— `reload_network_from_json` 合并完 parent 后立刻 `reset_network()`（重配即重建）。
- [src/testbed.cu:339-343](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L339-L343) —— `reload_network_from_file` 在**不是快照**的分支里 `reset_network()`；若是快照则跳过，保留权重（配合下一处）。
- [src/testbed.cu:5463](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L5463) —— 加载 `.ingp` 快照时以 `reset_network(false)` 建好网络骨架，紧接着在 5468 行 `m_trainer->deserialize(...)` 把已训练权重灌回去。
- [src/testbed.cu:4575-4576](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4575-L4576) —— `train()` 开头的「按需建网」兜底：若 `m_trainer` 为空（换了模式却没显式重载网络），就调 `reload_network_from_file()` 触发 `reset_network`。

而 `set_mode` 只负责**清空**、不负责重建：

[src/testbed.cu:207-216](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L207-L216) —— `set_mode` 把 `m_encoding/m_loss/m_network/m_nerf_network/m_optimizer/m_trainer` 统统赋空（`={}`），并把 `m_training_data_available=false`。这正是 u2-l1 所说「先清后设、建网推迟到 reset_network」的代码佐证。

#### 4.2.4 代码实践

1. **实践目标**：画出 `reset_network` 的事件时序，验证「Trainer 最后建、训练步归零」。
2. **操作步骤**：通读 [src/testbed.cu:4160-4412](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4160-L4412)，在笔记里列出 11 个步骤各自的关键行号。
3. **需要观察的现象**：确认 `m_training_step = 0`（4388 行）出现在 `new Trainer`（4383 行）之后、`envmap` 副模型（4392+）之前。
4. **预期结果**：你能解释「为什么加载快照必须在 `reset_network(false)` 之后立刻 `deserialize`」——因为前者把 step 归零、建了空权重网络，后者才有对象可灌权重。
5. **待本地验证**：若本地能编译运行，可在 `reset_network` 入口加一行 `tlog::info() << "reset_network called, clear_density_grid=" << clear_density_grid;`，然后分别执行「加载 fox」和「加载一个 .ingp 快照」，观察打印次数与 `clear_density_grid` 的真假差异。

#### 4.2.5 小练习与答案

**练习 1**：`reset_network(false)` 里的 `false` 传给 `clear_density_grid`。加载快照时为什么传 `false`？

> **参考答案**：快照里通常已经保存了训练好的密度网格状态（或希望保留几何先验）。传 `false` 跳过 `density_grid.memset(0)`，避免把已有的密度信息清掉；随后 `deserialize` 会恢复完整状态。

**练习 2**：`reload_network_from_file` 在加载快照分支里**不**调 `reset_network`，那网络对象从哪来？

> **参考答案**：来自稍后的 `load_snapshot` 流程——它会在 5463 行调 `reset_network(false)` 建好空网络骨架，再 `deserialize` 灌权重。也就是说「跳过」只是不在 `reload` 阶段重建，重建推迟到了快照加载流程里。

---

### 4.3 工厂函数与对象构造

#### 4.3.1 概念说明

工厂函数让 `reset_network` 不必关心「到底是 L2 还是 Huber、到底是 Adam 还是 SGD」。它只把 JSON 交给工厂，工厂根据 `otype` 字段返回正确的子类实例。在 `reset_network` 里直接调用的工厂有三个：

| 工厂 | 输入 | 产出 | 调用点 |
| --- | --- | --- | --- |
| `create_loss` | `loss_config` | `Loss<network_precision_t>` | NeRF 与非 NeRF 都调 |
| `create_optimizer` | `optimizer_config` | `Optimizer<network_precision_t>` | 同上 |
| `create_encoding` | `n_input, encoding_config` | `Encoding<network_precision_t>` | 仅非 NeRF/非 Takikawa 分支调 |

而**网络对象本身**不直接用某个 `create_network`，而是用两个「包装器」之一：

- **`NerfNetwork`**：NeRF 专用，内部包含 `pos_encoding + density_network + dir_encoding + rgb_network` 四件套（双头架构，详见 u4-l2）。
- **`NetworkWithInputEncoding`**：SDF / Image / Volume 通用，把一个 `Encoding` 与一个普通 `Network`（MLP）打包成「编码 → MLP」的端到端对象。

这两个包装器内部最终都会用 tiny-cuda-nn 的 MLP 工厂（`FullyFusedMLP` / `CutlassMLP` / `MegakernelMLP` 等，由 `network.otype` 决定）来实例化真正的全连接层——这一层在 tiny-cuda-nn 里，本仓库不直接调。

此外，`reset_network` 还构造了两个**副模型**（同样走 create_loss/create_optimizer/Trainer 三件套）：`distortion_map`（畸变图）与 `envmap`（环境贴图），它们是 NeRF 自标定的可训练缓冲，与主网络并行训练（详见 u8-l3）。

#### 4.3.2 核心流程

`create_optimizer` 如何「吃掉」嵌套 JSON？以 `configs/nerf/base.json` 的 optimizer 为例，它的结构是一棵嵌套树：

```
optimizer_config = {
  "otype": "Ema",            ← 最外层
  "decay": 0.95,
  "nested": {
    "otype": "ExponentialDecay",
    "decay_start": 20000, "decay_interval": 10000, "decay_base": 0.33,
    "nested": {
      "otype": "Adam",       ← 最内层，真正持有学习率
      "learning_rate": 1e-2, "beta1": 0.9, "beta2": 0.99, "epsilon": 1e-15, "l2_reg": 1e-6
    }
  }
}
```

`create_optimizer` 读到 `otype: "Ema"`，就 `new EmaOptimizer(...)`，并**递归地**用 `config["nested"]` 再调一次自己，构造内层的 `ExponentialDecay`，内层又递归构造最里层的 `Adam`。最终得到一个「洋葱式」对象：外层 EMA 平滑梯度、中层 ExponentialDecay 按步数衰减学习率、内层 Adam 真正更新参数。每次 `step` 时数据从外到内穿透，更新从内到外回传。

这种「装饰器/嵌套」模式同样出现在 `dir_encoding`（`Composite` + `nested` 数组）和 `distortion_map.optimizer`、`envmap.optimizer` 里。

#### 4.3.3 源码精读

`create_loss` 与 `create_optimizer` 的调用（工厂的直接使用点）：

[src/testbed.cu:4262-4263](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4262-L4263) —— `m_loss.reset(create_loss<network_precision_t>(loss_config));` 和 `m_optimizer.reset(create_optimizer<network_precision_t>(optimizer_config));`。注意 NeRF 分支里 `loss_config["otype"]` 在 4214 行被强制改写成 `"L2"`（因为部分 NeRF 损失类型不被 `Loss` 支持，NeRF 代码路径会绕过 `Loss` 自己算），所以这里 `create_loss` 看到的永远是 L2。

`create_encoding` 的调用（仅非 NeRF/非 Takikawa）：

[src/testbed.cu:4354](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4354) —— `m_encoding.reset(create_encoding<network_precision_t>(dims.n_input, encoding_config));`。而 NeRF 的 `m_encoding` 是从 `NerfNetwork` 里取出来的（`pos_encoding()`），不在这里建。

网络对象包装器之一：NeRF 分支用 `NerfNetwork`：

[src/testbed.cu:4282-4296](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4282-L4296) —— 遍历 `m_devices`，每个设备 `device.set_nerf_network(std::make_shared<NerfNetwork<network_precision_t>>(dims.n_pos, n_dir_dims, n_extra_dims, dims.n_pos+1, encoding_config, dir_encoding_config, network_config, rgb_network_config))`。`dims.n_pos+1` 那个偏移来自 `NerfCoordinate` 的 `dt` 成员（代码注释标了 `HACKY`）。`m_network` 与 `m_nerf_network` 同时指向主设备这份。

网络对象包装器之二：其他模式用 `NetworkWithInputEncoding`：

[src/testbed.cu:4362-4366](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4362-L4366) —— `device.set_network(std::make_shared<NetworkWithInputEncoding<network_precision_t>>(m_encoding, dims.n_output, network_config))`，把刚建好的 `m_encoding` 与 `network_config` 交给包装器，`m_network` 取主设备这份。

副模型：distortion_map 与 envmap（同样走 create_loss/create_optimizer/Trainer 三件套）：

[src/testbed.cu:4322-4326](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4322-L4326) —— `distortion_map` 副模型：`TrainableBuffer`（可训练缓冲）+ `create_optimizer` + `Trainer<float,float>`。

[src/testbed.cu:4400-4404](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4400-L4404) —— `envmap` 副模型：同样 `TrainableBuffer` + `create_optimizer` + `Trainer<float,float>`。这两个副模型与主网络共享同一套构造范式，证明「Loss/Optimizer/Trainer 三件套」是可复用的乐高积木。

#### 4.3.4 代码实践（本讲主实践）

> 任务：在 `reset_network` 中找到 `create_loss`/`create_optimizer` 的调用点，说明 optimizer 配置里的 `Ema → ExponentialDecay → Adam` 嵌套结构是如何被构造出来的。

1. **实践目标**：把「JSON 嵌套结构」与「工厂递归构造」对应起来。
2. **操作步骤**：
   - 打开 [src/testbed.cu:4262-4263](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4262-L4263)，确认 `create_optimizer<network_precision_t>(optimizer_config)` 这一行。
   - 打开 [configs/nerf/base.json:5-22](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/nerf/base.json#L5-L22)，画出三层 `nested` 的树状图（外 `Ema` → 中 `ExponentialDecay` → 内 `Adam`）。
   - 对照 [configs/sdf/base.json:5-22](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json#L5-L22)，比较两者的 `learning_rate`（NeRF `1e-2` vs SDF `1e-4`）与 `decay_start/decay_interval`（NeRF `20000/10000` vs SDF `10000/5000`）。
3. **需要观察的现象**：工厂函数的实现（递归读 `nested`）在 tiny-cuda-nn 里，但调用契约——「外层 otype 决定最外层包装、`nested` 键承载下一层」——在 instant-ngp 的 JSON 里清晰可见。
4. **预期结果**：你能口述「`create_optimizer` 看到 `otype:Ema` 就 new 一个 EMA 优化器，并用 `config["nested"]` 递归构造内层，直到最里层 `otype:Adam` 没有再嵌套为止」。
5. **拓展（待本地验证）**：拷贝 `configs/nerf/base.json`，把最外层 `Ema` 删掉（直接让顶层是 `ExponentialDecay`），用 pyngp 加载 fox 训练若干步，观察损失曲线是否变得更抖动（EMA 的作用正是平滑梯度）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `reset_network` 里**没有**直接调用某个 `create_network`？

> **参考答案**：因为 instant-ngp 用的不是裸 MLP，而是「编码 + MLP」的组合体。NeRF 用 `NerfNetwork`（双头，内部含 pos/dir 两套编码和密度/颜色两个 MLP），其他模式用 `NetworkWithInputEncoding`（单头，把一个 Encoding 与一个 MLP 打包）。这两个包装器内部再去实例化具体的 MLP（`FullyFusedMLP` 等），那一步才用到 tiny-cuda-nn 的 MLP 工厂。

**练习 2**：`alignment` 取 16 还是 8 由什么决定？为什么 `FullyFusedMLP` 需要 16？

> **参考答案**：由 `network_config["otype"]` 决定——`FullyFusedMLP`/`MegakernelMLP` 取 16，其余取 8（见 [src/testbed.cu:4329-4333](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4329-L4333)）。`FullyFusedMLP` 是手工写的极致融合内核，按 16 个样本/16 维特征成组处理以充满 GPU 的 warp/tile，因此要求输入特征维度对齐到 16；普通 MLP 用更通用的实现，8 对齐即可。（对齐细节的完整讨论见 u3-l3。）

**练习 3**：NeRF 分支里 `loss_config["otype"]` 为什么被改写成 `"L2"`？

> **参考答案**：NeRF 支持一些 `Loss` 基类不认识的损失类型（如 `RelativeL2` 之外的 NeRF 专用损失）。代码注释（4211–4213 行）说明：NeRF 训练路径会**绕过** `m_loss` 自己计算损失，所以这里只建一个占位用的 L2 `Loss`，真正的损失类型另存到 `m_nerf.training.loss_type`（4209 行）。

---

## 5. 综合实践

**任务：从一份 JSON 追踪到 `m_trainer` 的诞生。**

把本讲三个模块串起来，做一次完整的「配置 → 对象」追踪：

1. **读配置**：打开 [configs/sdf/base.json](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/configs/sdf/base.json)，确认它的 `encoding` 是 `HashGrid`、`network` 是 `FullyFusedMLP`、optimizer 是三层嵌套、loss 是 `MAPE`。
2. **走流程**：在脑中（或笔记里）把这四个 JSON 块送进 [src/testbed.cu:4192-4389](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4192-L4389)，按 `reset_network` 的步骤标注每一块的归宿：
   - `loss` → `create_loss` → `m_loss`（4262 行）
   - `optimizer` → `create_optimizer` → `m_optimizer`（4263 行）
   - `encoding`（HashGrid）→ 触发 4219–4259 的自动参数推导，最终 `create_encoding` → `m_encoding`（4354 行）
   - `network`（FullyFusedMLP）→ `alignment=16`（4329 行），与 `m_encoding` 一起 `make_shared<NetworkWithInputEncoding>` → `m_network`（4363 行）
3. **装总指挥**：最后四者 + `m_seed` → `m_trainer`（4383 行），`m_training_step=0`（4388 行）。
4. **回答三个问题**：
   - 这套对象里，哪个是「洋葱式」由三层嵌套构造的？（答：`m_optimizer`。）
   - 为什么 SDF 的 `m_network` 不是 `NerfNetwork`？（答：非 NeRF 模式走 `NetworkWithInputEncoding` 包装器。）
   - `LOSS_SCALE` 在哪一步被用上？（答：不在 `reset_network` 里出现，它在训练步算损失时放大损失、在优化器更新时除掉；`reset_network` 只负责把对象建出来。）
5. **进阶（待本地验证）**：若已编译 pyngp，写 5 行脚本 `Testbed(TestbedMode.Sdf)` → `load_training_data("data/sdf/armadillo.obj")` → `reload_network_from_file("base")`，在终端捕获 `reset_network` 打印的 `Model: ...` 与 `MultiLevelEncoding: ...` 两行日志，与你的追踪结果对账。

## 6. 本讲小结

- **五个模型对象**：`m_loss / m_optimizer / m_encoding / m_network / m_trainer` 是 Testbed 持有的网络「五件套」，声明在 [testbed.h:1236-1240](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/include/neural-graphics-primitives/testbed.h#L1236-L1240)；`m_trainer` 最后建，因为它要握住另外三者。
- **reset_network 是总装车间**：它重置训练临时状态、从 JSON 取四大块、按模式分发构造网络、最后装配 Trainer，并把 `m_training_step` 归零（从头训练）。
- **构造依赖顺序**：`create_loss`/`create_optimizer` 先建 → `m_encoding`/`m_network`（按模式走 `NerfNetwork` 或 `NetworkWithInputEncoding`）→ `Trainer` 最后 `new`。
- **工厂递归**：`create_optimizer` 按嵌套 JSON 递归构造「Ema → ExponentialDecay → Adam」洋葱式优化器，`create_loss`/`create_encoding` 同理按 `otype` 选子类。
- **按需重建**：`set_mode` 只清空、不建网；真正建网发生在 `reset_network`，它由 `reload_network_from_file/json`、`train()` 的 `!m_trainer` 兜底、加载快照等场景触发。
- **LOSS_SCALE**：混合精度下为防梯度下溢，把损失整体放大的编译期常量（值由 tiny-cuda-nn 的 `default_loss_scale` 给出），在 `reset_network` 不直接使用，而在训练/优化器路径中生效。

## 7. 下一步学习建议

- **下一讲 u3-l2（多分辨率哈希编码）**：本讲把网格编码的自动参数推导（`per_level_scale` 公式、`n_levels`、`log2_hashmap_size`）一笔带过，下一讲会从论文思想到公式逐行拆解 [src/testbed.cu:4219-4259](https://github.com/NVlabs/instant-ngp/blob/abe236ee00cf90cfca6e36e65c00435d5b21f50a/src/testbed.cu#L4219-L4259) 那段代码。
- **u3-l3（网络构建与 FullyFusedMLP）**：深入本讲提到的 `alignment` 16/8 对齐、`NetworkWithInputEncoding` 内部如何拼装 MLP。
- **u4-l2（NerfNetwork 双头架构）**：本讲把 `NerfNetwork` 当黑盒，那一讲会拆开它的 `pos_encoding/density_network/dir_encoding/rgb_network` 四件套与前向流程。
- **u8-l3（相机位姿与镜头优化）**：本讲提到的 `distortion_map`、`envmap` 两个副模型在那里有完整说明。
