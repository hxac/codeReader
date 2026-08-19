# 适配器选择算法：混合 GPU 下的四级排序与实测验证

## 1. 本讲目标

上一讲（u2-l1）我们搞清楚了 `WgpuContext` 如何创建 instance / adapter / device / queue 四层对象。当时留了一个问题：「适配器是怎么选出来的？」本讲就专门回答它。学完本讲，你应该能够：

1. 说清楚为什么在混合 GPU（双显卡）系统上，"查询能力"和"真的能用"是两回事，以及 `select_adapter_and_device` 如何应对这个难题。
2. 逐级复述排序的四级优先键：`ZED_DEVICE_ID` 用户覆盖 > 合成器 GPU 提示 > 设备类型 > 后端优先级，并能手推任何一个适配器在这四级上的得分元组。
3. 解释 `try_adapter_with_surface` 为什么要「真的配一次表面」来验证兼容性，以及 error scope 在其中起的作用。
4. 会用 `ZED_DEVICE_ID` 环境变量强制指定显卡，并知道它的格式约束（可选 `0x` 前缀 + 恰好 4 个十六进制字符）与失效场景（OpenGL 适配器上报 `device=0`）。

## 2. 前置知识

- **适配器（Adapter）回顾**：wgpu 中 `Adapter` 代表一块可以被使用的物理显卡（或驱动模拟的显卡）。`Instance::enumerate_adapters` 能列出系统里所有可见适配器，每个都带一份 `AdapterInfo`（名称、vendor、device、backend、device_type）。这是本讲排序的原始数据。
- **混合 GPU（hybrid / dual graphics）**：许多笔记本同时装着 Intel/AMD 核显和 NVIDIA 独显。Linux 显示服务器（Wayland 合成器或 X server）通常只在其中一块卡上做合成（compositing），应用画的东西要经过它才能上屏。如果应用选了"另一块卡"，画面就需要跨卡复制，甚至在某些驱动组合下直接配置失败。
- **PCI vendor ID / device ID**：每块 PCI 设备都有一对 16 位标识，vendor 标厂商（如 `0x8086` 是 Intel、`0x10de` 是 NVIDIA），device 标具体型号。Linux 上可以通过 sysfs（`/sys/dev/char/.../device/vendor` 与 `.../device`）查到渲染节点对应的这对 ID。`CompositorGpuHint` 装的就是它。
- **表面（Surface）与能力（capabilities）**：`wgpu::Surface` 是"可以往哪个窗口/canvas 输出画面"的抽象。`surface.get_capabilities(&adapter)` 返回某个适配器对这个表面支持的格式、alpha 模式等列表——这是"纸面兼容性"。本讲的核心教训之一就是：纸面兼容 ≠ 实际可用。
- **Rust 的 `sort_by_key` 与稳定排序**：`Vec::sort_by_key` 是稳定排序——两个元素的关键字完全相等时，保持它们在原向量中的相对顺序。本讲的排序键是一个四元组，元组比较是字典序（先比第一个，相等再比第二个，以此类推）。这两点是手推排序结果时的全部规则。
- **`wgpu::DeviceType`**：适配器的"种类"枚举——`DiscreteGpu`（独立显卡）、`IntegratedGpu`（集成在 CPU 里的核显）、`VirtualGpu`（虚拟设备）、`Cpu`（纯软件模拟，如 llvmpipe）、`Other`（其他，OpenGL 后端常落在这里）。

## 3. 本讲源码地图

| 文件 | 本讲关注的区域 | 作用 |
| --- | --- | --- |
| [src/wgpu_context.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L1-L606) | L321-L455 `select_adapter_and_device` | 本讲主角：枚举、排序、逐个实测适配器 |
| 同上 | L460-L500 `try_adapter_with_surface` | 用真实表面配置验证单个适配器 |
| 同上 | L351-L397 `sort_by_key` 闭包 | 四级排序键的计算 |
| 同上 | L59-L63 `CompositorGpuHint` | 合成器 GPU 提示的数据结构 |
| 同上 | L91-L101（`new_with_options` 开头） | 读取并解析 `ZED_DEVICE_ID` |
| 同上 | L568-L583 `parse_pci_id`、L589-L605 测试 | PCI ID 字符串解析及其单元测试 |
| 同上 | L289-L298 `WgpuContext::instance` | 原生 instance 工厂（只启用 Vulkan \| GL，影响排序第四级的实际取值） |
| [crates/gpui_linux/src/linux/platform.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/platform.rs#L1218-L1256) | L1218-L1256 `compositor_gpu_hint_from_dev_t` | `CompositorGpuHint` 的"生产端"：把显示服务器设备的 `dev_t` 换算成 PCI ID（仅作背景，u6-l3 详讲） |

本讲只精读 `wgpu_context.rs` 一个文件；gpui_linux 那一段只用来回答「提示从哪来」。

## 4. 核心概念与源码讲解

### 4.1 select_adapter_and_device：枚举 → 排序 → 逐个实测的总体策略

#### 4.1.1 概念说明

选适配器最"标准"的 wgpu 写法是 `instance.request_adapter(&RequestAdapterOptions { compatible_surface: Some(&surface), .. })`——把决定权交给驱动。但这个 API 只回一个结果，不给解释、不给重试机会，而且在混合 GPU 系统上它参考的信息（电源偏好等）并不包含"合成器实际在用哪块卡"。

本 crate 的策略分三步，本质上是**自己接管选择权**：

1. **枚举**：把系统里所有适配器全部列出来，不遗漏；
2. **排序**：用一个四级优先键把"最想用的"排到最前面；
3. **实测**：按排序后的顺序逐个尝试——真的创建 device、真的把 surface 配置一次，谁先通过就用谁；全挂了才报错。

这套流程回答的问题是：「在这些卡里，哪一块既能满足我的偏好，又能真正驱动这个窗口？」

#### 4.1.2 核心流程

```text
new_with_options()
  ├─ 读取环境变量 ZED_DEVICE_ID → parse_pci_id → Option<u32>（坏值只记日志、不崩溃）
  └─ block_on(select_adapter_and_device(instance, device_id_filter, surface, compositor_gpu, reject_software))
       ├─ 1. instance.enumerate_adapters(Backends::all())   → 空则直接 bail
       ├─ 2. adapters.sort_by_key(四级优先键元组)
       ├─ 3. log::info 打印排序后的完整适配器清单
       └─ 4. for adapter in adapters（按优先级顺序）
            ├─ reject_software 且 DeviceType::Cpu → 跳过
            ├─ try_adapter_with_surface(adapter, surface)
            │    ├─ 成功 → 返回 (adapter, device, queue, dual_source_blending, color_format)
            │    └─ 失败 → log::info 记录失败原因，继续下一个
            └─ 循环结束仍无成功 → bail "No GPU adapter found that can configure the display surface"
```

注意第 4 步的一个细节：**排序只决定尝试顺序，不直接决定结果**。排序第一名如果实测失败，会自然落到第二名。排序是"偏好"，实测是"裁判"。

#### 4.1.3 源码精读

函数的文档注释直接点明了动机，值得整段读：

- [src/wgpu_context.rs:L315-L319](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L315-L319)：注释说明这是在混合 GPU 系统上确定兼容性的唯一可靠办法——适配器可能通过 `get_capabilities()` 报告支持表面，但实际配置时失败（典型例子：NVIDIA 声称支持 Vulkan Wayland，但 Wayland 合成器跑在 Intel GPU 上）。

入口侧，`new_with_options` 先解析环境变量再调用本函数：

- [src/wgpu_context.rs:L91-L101](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L91-L101)：读取 `ZED_DEVICE_ID`。解析失败时用 `.log_err()` 把 `Result` 降级成 `None`——也就是说，用户写了个非法值不会让程序崩溃，只是覆盖失效，这一点和"宁可降级不失败"的保守风格一致（u2-l1 讲过同样的思路）。
- [src/wgpu_context.rs:L103-L112](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L103-L112)：注释再次强调"用真实设备测试表面配置"，然后 `gpui::block_on` 阻塞执行异步的 `select_adapter_and_device`，五个返回值（adapter、device、queue、双源混合能力、色彩纹理格式）一一透传。

函数主体：

- [src/wgpu_context.rs:L334-L338](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L334-L338)：`instance.enumerate_adapters(wgpu::Backends::all()).await` 枚举全部适配器；一个都没有就直接报错退出。这里的 `Backends::all()` 只是"不过滤"的意思——实际能枚举出哪些后端取决于 instance 创建时启用了什么（见 4.2.3 最后一点）。
- [src/wgpu_context.rs:L340-L342](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L340-L342)：如果设置了 `ZED_DEVICE_ID` 过滤，打印一条 `ZED_DEVICE_ID filter: 0x....` 日志，方便用户确认覆盖已生效。
- [src/wgpu_context.rs:L399-L411](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L399-L411)：排序完成后，把**全部**适配器按新顺序打进日志（名称、vendor、device、backend、type）。这行日志是本讲实践任务的重要观察点——它让你不用调试器就能看到排序结果。
- [src/wgpu_context.rs:L413-L424](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L413-L424)：实测循环开头。若调用方是 `new_rejecting_software`（设备恢复场景，u6-l1 详讲），软件渲染器（`DeviceType::Cpu`）会被直接跳过。
- [src/wgpu_context.rs:L426-L452](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L426-L452)：对每个候选打印 `Testing adapter: ...`，调用 `try_adapter_with_surface`；成功则打印 `Selected GPU (passed configuration test)` 并连同 device/queue 一起返回，失败则打印原因继续。
- [src/wgpu_context.rs:L454](https://github.com/zed-industries-zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L454)：全部失败的兜底错误信息。

#### 4.1.4 代码实践：跟踪一遍日志链

1. **实践目标**：不看源码也能从日志读懂"Zed 选卡"的全过程，为后续实验（4.4.4）打基础。
2. **操作步骤**：
   - 在源码中定位以下 5 条日志的打印位置：`ZED_DEVICE_ID filter`（L341）、`Found {} GPU adapter(s)`（L400）、`  - {} (vendor=...)`（L403）、`Testing adapter`（L426）、`Selected GPU (passed configuration test)`（L430-L434），以及失败分支的 `failed: ..., trying next...`（L444-L449）。
   - 把它们按时间顺序抄成一张"启动日志模板"，并在每条旁边标注它出现在流程图的哪一步。
3. **需要观察的现象**：一张普通双显卡机器上的理想日志应该是——先列出 N 个适配器（已按优先级排序），然后依次 `Testing adapter`，直到某一条后面跟着 `Selected GPU (passed configuration test)`。
4. **预期结果**：你会得到一份"日志 ↔ 代码"对照表；之后任何 Zed 选卡问题（选错卡、启动黑屏）都可以先拿这张表对着日志定位到具体分支。真实机器上的日志内容**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `select_adapter_and_device` 不直接用 `request_adapter`，而要自己枚举 + 排序 + 逐个试？

**参考答案**：`request_adapter` 是一个"黑盒决策"：只能表达电源偏好等粗粒度意图，拿不到候选列表，无法注入"合成器在哪块卡"这类关键信息，失败时也没有"换下一块试试"的机会。自己枚举才能看到全部候选并按业务语义（用户覆盖、合成器位置、卡型、后端）排序，自己实测才能在混合 GPU 假阳性下逐级降级，最终还可以把"为什么选它/为什么跳过它"全部写进日志。

**练习 2**：如果系统里只有一个适配器且它实测失败，用户会看到什么？

**参考答案**：循环只跑一次，打印 `Testing adapter: ...` 和失败原因（`failed: ..., trying next...`），随后落到 [L454](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L454) 的 `anyhow::bail!("No GPU adapter found that can configure the display surface")`，错误沿 `new_with_options` → 调用方（`WgpuRenderer::new`）向上传播，窗口创建失败。

### 4.2 四级排序键：sort_by_key 的优先级语义

#### 4.2.1 概念说明

排序的输入是每个适配器的 `AdapterInfo`，输出是一个 `(u8, u8, u8, u8)` 四元组；元组按字典序比较，值越小越靠前。四个分量从高到低是：

| 级别 | 分量 | 含义 | 取 0 的条件 |
| --- | --- | --- | --- |
| 1 | `user_override` | 用户是否用 `ZED_DEVICE_ID` 显式点名这张卡 | 点名命中（且该卡 device ID 已知非 0） |
| 2 | `compositor_match` | 是否是显示服务器正在用于合成的那块卡 | vendor 与 device 同时命中 `CompositorGpuHint` |
| 3 | `type_priority` | 设备类型偏好 | DiscreteGpu（0）< IntegratedGpu（1）< Other（2）< VirtualGpu（3）< Cpu（4） |
| 4 | `backend_priority` | 图形 API 后端偏好 | Vulkan / Metal / Dx12（0），其余（1，如 GL） |

语义上：**用户的明确意志 > 系统的实际约束 > 性能直觉 > API 偏好**。第一级压倒一切——哪怕用户点名的是一块核显，也排在最前；第二级压倒第三级——"合成器在用的核显"会排在"没被点名的独显"前面，因为跨卡渲染的代价与风险比卡本身强弱更重要。

还有一个容易忽略的前提变量 `device_known`：**OpenGL 这类后端会给所有适配器上报 `device = 0`**，此时任何基于 device 的比较都没有意义，所以第一、二级都要求 `device_known` 为真才可能命中。

#### 4.2.2 核心流程

对每个适配器计算得分元组的伪代码：

```text
score(adapter):
    info         = adapter.get_info()
    device_known = (info.device != 0)          # GL 后端常报 0

    user_override =
        if device_id_filter == Some(id) and device_known and info.device == id
        then 0 else 1                          # 注意：只比 device，不比 vendor

    compositor_match =
        if compositor_gpu == Some(hint) and device_known
           and info.vendor == hint.vendor_id and info.device == hint.device_id
        then 0 else 1                          # vendor 与 device 必须同时命中

    type_priority =                            # 越小越优先
        DiscreteGpu → 0; IntegratedGpu → 1; Other → 2; VirtualGpu → 3; Cpu → 4

    backend_priority =
        Vulkan | Metal | Dx12 → 0; 其他（GL 等）→ 1

    return (user_override, compositor_match, type_priority, backend_priority)

排序 = 对全部适配器按 score 升序做稳定排序（同分保持枚举顺序）
```

#### 4.2.3 源码精读

排序注释本身就是设计文档，四个层级一目了然：

- [src/wgpu_context.rs:L344-L350](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L344-L350)：四级优先级的注释。第三级还有一条经验之谈——`Other` 排在 `VirtualGpu` 之前，因为 OpenGL 适配器在 wgpu 里常被归类为 `Other`。

四个分量的实现：

- [src/wgpu_context.rs:L353-L356](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L353-L356)：先取 `info` 并计算 `device_known`，注释解释了原因——OpenGL 后端对所有适配器上报 `device=0`，基于 device 的匹配只在非 0 时有意义。
- [src/wgpu_context.rs:L358-L361](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L358-L361)：第一级 `user_override`。匹配条件是 `device_known && info.device == id`——只比较 device ID，不比较 vendor；若两张不同厂商的卡撞了同一个 device ID，会同时命中（谁在前取决于稳定排序）。
- [src/wgpu_context.rs:L363-L372](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L363-L372)：第二级 `compositor_match`，要求 vendor 与 device **同时**等于提示值。
- [src/wgpu_context.rs:L374-L384](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L374-L384)：第三级 `type_priority`。阅读小提示：外层 `if device_type == Cpu { 4 } else { match ... }` 看起来与 match 里的 `Cpu => 4` 重复，但 match 必须穷尽所有变体才能编译，所以内层那个 `Cpu => 4` 分支是编译器要求的完整性兜底，实际执行永远走外层 if。
- [src/wgpu_context.rs:L386-L389](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L386-L389)：第四级 `backend_priority`，偏好 Vulkan/Metal/Dx12。结合 [src/wgpu_context.rs:L289-L298](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L289-L298) 的 `WgpuContext::instance`（原生 instance 只启用 `VULKAN | GL`）可知：在这条链路上 Metal/Dx12 实际枚举不出来，写进优先级表只是表达一种与平台无关的通用偏好；真正起作用的是"Vulkan 优于 GL"。
- [src/wgpu_context.rs:L391-L396](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L391-L396)：把四个分量组装成元组返回给 `sort_by_key`。

#### 4.2.4 代码实践：手推一次四级排序

1. **实践目标**：给定一组假设适配器，独立算出每个的 `(user_override, compositor_match, type_priority, backend_priority)` 元组并排出最终顺序，验证你真正理解了优先级语义。
2. **操作步骤**：
   - 设定场景：Wayland 桌面，合成器跑在 Intel 核显上，即 `compositor_gpu = Some(CompositorGpuHint { vendor_id: 0x8086, device_id: 0x46a6 })`，未设置 `ZED_DEVICE_ID`。
   - 假设 `enumerate_adapters` 按下表顺序返回 5 个适配器（**以下均为假设数据，仅用于推演**）：

| # | 名称 | vendor | device | backend | device_type |
| --- | --- | --- | --- | --- | --- |
| A | NVIDIA RTX 4060 | 0x10de | 0x2404 | Vulkan | DiscreteGpu |
| B | Intel Iris Xe | 0x8086 | 0x46a6 | Vulkan | IntegratedGpu |
| C | AMD RX 7600 | 0x1002 | 0x7480 | Vulkan | DiscreteGpu |
| D | llvmpipe | 0xffff | 0x0000 | Vulkan | Cpu |
| E | NVIDIA RTX 4060 (GL) | 0x10de | 0x0000 | GL | Other |

   - 对每一行计算四元组（注意 D、E 的 `device = 0` 意味着什么），写出排序后的顺序。
   - 重新推演：设置 `ZED_DEVICE_ID=0x2404` 后顺序如何变化？再设 `ZED_DEVICE_ID=0x7777`（无人匹配）呢？
3. **需要观察的现象**：第二级是否压倒了第三级（核显 B 排到独显 A、C 前面）；稳定排序下 A、C 同分时的先后；`device=0` 的适配器在第一、二级上的表现。
4. **预期结果**：参见 4.2.5 练习 1 的参考答案，逐项核对。

#### 4.2.5 小练习与答案

**练习 1**：给出 4.2.4 场景中 5 个适配器的得分元组与最终排序。

**参考答案**：

| # | device_known | 元组 | 排序后位置 |
| --- | --- | --- | --- |
| A | 是 | (1, 1, 0, 0) | 2 |
| B | 是 | (1, **0**, 1, 0) | **1** |
| C | 是 | (1, 1, 0, 0) | 3 |
| D | 否（device=0） | (1, 1, 4, 0) | 5 |
| E | 否（device=0） | (1, 1, 2, 1) | 4 |

最终顺序：**B → A → C → E → D**。三个关键点：(1) B 的核显身份（type=1）虽然劣于独显（type=0），但第二级 compositor_match=0 直接胜出——"合成器在用"比"卡更强"优先；(2) A 与 C 四个分量全同，稳定排序保持枚举顺序 A 在前；(3) D 的 backend 是 Vulkan（第 4 级=0），但第 3 级 Cpu=4 使它垫底。

设置 `ZED_DEVICE_ID=0x2404` 后：A 的 `user_override` 变 0，元组变为 (0,1,0,0)，跳到第一，顺序 **A → B → C → E → D**。设置 `ZED_DEVICE_ID=0x7777` 时无人命中，全部 `user_override=1`，顺序不变——一个不存在的 device ID 等于没有覆盖。

**练习 2**：为什么 `user_override` 和 `compositor_match` 都要先检查 `device_known`？

**参考答案**：OpenGL 一类后端会对所有适配器上报 `device=0`。若不做这个守卫，`ZED_DEVICE_ID=0`（或提示里 device 恰为 0）会让所有 GL 适配器"全部命中"第一/第二级，排序被无意义地扰动。要求 `device_known` 保证这两级只在设备标识真实可用时参与比较（[src/wgpu_context.rs:L353-L356](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L353-L356)）。

**练习 3**：想用 `ZED_DEVICE_ID` 强制选用某个纯 OpenGL 适配器（如上表的 E），能成功吗？

**参考答案**：不能通过第一级实现。E 上报 `device=0`，`device_known=false`，无论 `ZED_DEVICE_ID` 填什么值它的 `user_override` 都是 1——用户覆盖对这类适配器是**静默无效**的（不会报错，只是不生效）。它只能靠后续级别排序，而 GL 后端在第 4 级又是劣先。这也是排查"设了环境变量没反应"时首先要检查的点。

### 4.3 try_adapter_with_surface：用真实配置测试兼容性

#### 4.3.1 概念说明

排序第一名只是"最有希望"，不一定"真的能用"。`get_capabilities()` 返回的格式列表来自驱动的静态声明，而混合 GPU 系统上存在**假阳性**：适配器声称支持某种表面格式，实际 configure 时却因为跨设备呈现而失败。本函数的做法是干脆利落——**真刀真枪配一次**：

1. 先做两个便宜的静态检查（格式列表、alpha 模式列表非空），不过关就不浪费一次 device 创建；
2. 真的创建 device 和 queue（复用 `create_device`，即 u2-l1 精读过的保守申请逻辑）；
3. 推入一个**验证错误捕获域（error scope）**，用一份 64×64、Fifo present mode 的最小 `SurfaceConfiguration` 真的调用 `surface.configure`；
4. 弹出 error scope：捕获到验证错误就判定失败；干净则判定成功，并把已经创建好的 device/queue 直接返回复用——**测试过程零浪费**。

error scope 是 wgpu 的异步错误捕获机制：验证类错误不会以 `Result` 形式从 `configure` 返回，而是进入错误回调/错误域；`push_error_scope(ErrorFilter::Validation)` + `pop().await` 的组合能把"这一段操作里有没有发生验证错误"变成一个可等待的 `Option<Error>`。

#### 4.3.2 核心流程

```text
try_adapter_with_surface(adapter, surface):
    caps = surface.get_capabilities(adapter)
    if caps.formats.is_empty()     → 失败："no compatible surface formats"（静态排除）
    if caps.alpha_modes.is_empty() → 失败："no compatible alpha modes"（静态排除）

    (device, queue, ...) = create_device(adapter)      # 失败直接向上传播
    error_scope = device.push_error_scope(Validation)  # 开始捕获验证错误

    surface.configure(&device, 64×64 / caps.formats[0] / Fifo / caps.alpha_modes[0])

    if error_scope.pop().await == Some(e) → 失败："surface configuration failed: {e}"
    否则 → 成功，返回 (device, queue, ...)            # 测试产物直接复用
```

#### 4.3.3 源码精读

- [src/wgpu_context.rs:L457-L458](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L457-L458)：文档注释点明函数价值——成功时返回 device 和 queue 供复用。
- [src/wgpu_context.rs:L464-L470](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L464-L470)：两道静态门槛。`formats` 或 `alpha_modes` 为空说明驱动纸面上就不支持，直接 bail，避免白建一次 device。
- [src/wgpu_context.rs:L472-L473](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L472-L473)：调用 u2-l1 讲过的 `create_device`（探测双源混合特性、保守 limits、请求 device），失败自然向上传播。
- [src/wgpu_context.rs:L474](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L474)：推入验证错误捕获域。
- [src/wgpu_context.rs:L476-L487](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L476-L487)：构造一份最小测试配置并真的 `surface.configure`。细节：尺寸只用 64×64（测试目的不需要真实窗口大小）；`format` 与 `alpha_mode` 都取能力列表的第一个（此处只验证"能不能配上"，不挑最优——挑最优是渲染器 `new_internal` 阶段的事，见 u3-l1）；`present_mode` 用到处支持的 `Fifo`。
- [src/wgpu_context.rs:L489-L492](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L489-L492)：弹出 error scope，捕获到错误即判定该适配器不可用。这一步就是"实测裁判"——静态能力查询永远发现不了这类失败。
- [src/wgpu_context.rs:L494-L499](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L494-L499)：成功时返回的正是刚创建的 device/queue，调用方（`select_adapter_and_device`）原样透传，**配置测试顺便完成了资源创建**。

顺带一提，[src/wgpu_context.rs:L300-L313](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L300-L313) 还有一个只查 `get_capabilities` 的 `check_compatible_with_surface`，用于多窗口复用已有上下文时的快速校验——它和本函数的"轻查 / 重测"分工将在 u2-l4 展开。

#### 4.3.4 代码实践：对比"轻查"与"重测"两条路径

1. **实践目标**：理解同一份 `caps.formats.is_empty()` 检查出现在两个函数里，分别防御什么。
2. **操作步骤**：
   - 通读 `try_adapter_with_surface`（L460-L500）与 `check_compatible_with_surface`（L300-L313），各画一张小流程图。
   - 在两张图上标注：哪个函数会创建 device、哪个会真的 configure、哪个可能产生副作用（device 创建、表面被临时配置成 64×64）。
3. **需要观察的现象**：`check_compatible_with_surface` 是纯只读探测；`try_adapter_with_surface` 有真实副作用——测试后表面被配置成了 64×64 的测试参数（后续真实渲染前会被渲染器重新 configure 覆盖，见 u3-l1）。
4. **预期结果**：能说清"为什么逐个试卡时必须用重测、而复用上下文时轻查就够"——试卡要防的是配置期假阳性；复用时上下文已经通过重测，只需确认新表面与该适配器有交集。GPU 相关行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么测试配置要用 64×64 和 `Fifo`，而不是直接用窗口的真实尺寸和最优 present mode？

**参考答案**：本函数的目的是**判定兼容性**，不是完成真实渲染配置。64×64 足以触发完整的配置校验路径，又把开销降到最低；`Fifo` 是唯一在所有平台上保证存在的 present mode，不会因为它 unsupported 而引入无关失败。真实尺寸、最优格式/present mode 的选择属于渲染器 `new_internal` 阶段的职责（u3-l1）。

**练习 2**：如果不推 error scope，`surface.configure` 失败时会发生什么？

**参考答案**：wgpu 的验证错误不会让 `configure` 返回 `Err`（它没有 `Result` 返回值），错误只会进入设备的错误处理通道。不推 error scope 的话，"配置失败了"这件事在本函数里根本观察不到——测试会"假装通过"，把真正不可用的适配器当成选中者返回，最后在真实渲染时才暴露为更难排查的故障。error scope 把异步验证错误变成可等待的 `Option<Error>`，这是整个"实测"策略成立的技术基础（[src/wgpu_context.rs:L474](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L474)、[L489-L492](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L489-L492)）。

### 4.4 CompositorGpuHint 与 parse_pci_id：提示的生产与用户覆盖的解析

#### 4.4.1 概念说明

排序的第一、二级都需要外部输入：

- **第二级的输入 `CompositorGpuHint`**：一对 `(vendor_id, device_id)`，由平台层（gpui_linux）从显示服务器信息换算而来。直觉是：Wayland 合成器 / X server 把哪个渲染设备节点（`/dev/dri/renderD128` 之类）用于合成，就提示选那块卡，避免跨卡复制与配置失败。
- **第一级的输入 `ZED_DEVICE_ID`**：一个环境变量，用户手填的 4 位十六进制 PCI device ID，用于"我知道我在干什么"的强制覆盖——比如强制独显跑测试、绕开某块驱动的 bug。

`parse_pci_id` 负责把用户填的字符串变成 `u32`，规则是：允许 `0x`/`0X` 前缀（可选），剩余部分必须**恰好 4 个十六进制字符**。`123`、`12345`、`xyz` 都非法；`ABCD`、`abcd`、`0xABCD` 都合法且等值。非法值的后果是记一条错误日志然后当作未设置（降级不崩溃）。

#### 4.4.2 核心流程

```text
提示链（第二级）：
  显示服务器设备的 dev_t
    → dev_major/dev_minor 拆码
    → 读 /sys/dev/char/{major}:{minor}/device/{vendor,device}
    → CompositorGpuHint { vendor_id, device_id }          # 读不到则 None
    → 存入平台 client 状态 → 传入 WgpuContext::new
    → select_adapter_and_device 的第二级排序键

覆盖链（第一级）：
  环境变量 ZED_DEVICE_ID
    → parse_pci_id：去 0x 前缀 + 校验恰 4 个 hex 字符 → u32
    → Some(id) 作为 device_id_filter 传入
    → 第一级排序键；命中日志 "ZED_DEVICE_ID filter: 0x...."
```

#### 4.4.3 源码精读

- [src/wgpu_context.rs:L59-L63](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L59-L63)：`CompositorGpuHint` 定义——一个 `Copy` 的两个字段结构体，只作为排序提示存在，不参与任何资源创建。
- [crates/gpui_linux/src/linux/platform.rs:L1218-L1256](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/platform.rs#L1218-L1256)：提示的生产端 `compositor_gpu_hint_from_dev_t`。用经典位运算从 `dev_t` 拆出主/次设备号，再去 sysfs 读这对 PCI ID；任何一步读不到就返回 `None`（提示缺失只影响排序质量，不影响可用性）。**本 crate 不做这件事——它只定义结构体和消费它，生产在平台层**，这是"接口薄、实现厚"分层的又一体现。
- [src/wgpu_context.rs:L91-L101](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L91-L101)：`new_with_options` 开头读 `ZED_DEVICE_ID`。三个分支：正常解析 → `Some(id)`；未设置 → `None`；读取/解析失败 → 记日志后 `None`。
- [src/wgpu_context.rs:L568-L583](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L568-L583)：`parse_pci_id` 全文。先 trim、再去 `0x`/`0X` 前缀，然后同时校验"全是十六进制字符"与"长度恰为 4"，最后 `u32::from_str_radix(id, 16)`。
- [src/wgpu_context.rs:L589-L605](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L589-L605)：单元测试 `test_parse_device_id`，断言 `0xABCD`/`ABCD`/`abcd`/`1234` 合法、`123` 非法，且大小写、有无 `0x` 前缀解析等值。这是本 crate 里少数无需 GPU 即可运行的测试之一。

#### 4.4.4 代码实践：验证 ZED_DEVICE_ID 的解析与生效

本实践分两部分：一部分纯 CPU、必可运行；一部分需要真实桌面环境，标注待本地验证。

1. **实践目标**：确认 `parse_pci_id` 的格式规则；在真实环境中观察 `ZED_DEVICE_ID` 过滤日志是否出现。
2. **操作步骤**：
   - **步骤 A（纯 CPU，可复现）**：在本仓库运行 `cargo test -p gpui_wgpu test_parse_device_id`，确认测试通过。再阅读 [L589-L605](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L589-L605) 的断言列表，把它们抄成一张"输入 → 预期"表。
   - **步骤 B（需要 Linux 桌面 + Zed，待本地验证）**：先用 `lspci -nn | grep -i vga`（或 `ls /sys/class/drm/` 配合 sysfs 的 `device/device` 文件）查出你某块卡的 4 位十六进制 device ID；然后以 `ZED_DEVICE_ID=0x<你的ID>` 启动 Zed（从终端启动以便看到日志，或查看 Zed 日志文件，Linux 下通常位于 `~/.local/share/Zed/logs/`）。
   - **步骤 C（对照组，待本地验证）**：分别用 `ZED_DEVICE_ID=zzzz`（格式非法）与 `ZED_DEVICE_ID=0x7777`（格式合法但无卡匹配）再启动一次，对比日志差异。
3. **需要观察的现象**：
   - 步骤 B：日志中应出现 `ZED_DEVICE_ID filter: 0x....`（[L341](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L340-L342)），且 `Found N GPU adapter(s)` 列表里被点名的卡排在首位，随后 `Testing adapter` 从它开始。
   - 步骤 C：非法值应出现解析错误日志但程序照常启动（排序不受影响）；合法无匹配值则连 `ZED_DEVICE_ID filter` 行都不会有实质排序变化（filter 日志仍会打印，但无人命中第一级）。
4. **预期结果**：步骤 A 测试通过；步骤 B/C 的日志行为与 4.2 的推演一致。**B、C 依赖真实 GPU 环境与日志级别，待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：`ZED_DEVICE_ID=0xAB`、`ZED_DEVICE_ID=0xABCD`、`ZED_DEVICE_ID=abcd`、`ZED_DEVICE_ID=0xABCDEFG` 分别会被解析成什么？

**参考答案**：`0xAB` → `Err`（去掉前缀后只有 2 个字符，不满足"恰好 4 个"）；`0xABCD` → `Ok(0xABCD)`；`abcd` → `Ok(0xABCD)`（前缀可选、大小写不敏感）；`0xABCDEFG` → `Err`（6 个字符）。解析失败的共同后果：记一条错误日志，`device_id_filter` 为 `None`，启动继续（[src/wgpu_context.rs:L91-L101](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L91-L101)）。

**练习 2**：`CompositorGpuHint` 为什么定义在 gpui_wgpu，而它的生产逻辑（读 sysfs）却在 gpui_linux？

**参考答案**：`CompositorGpuHint` 是"渲染器选卡"这一抽象的输入参数类型，属于 gpui_wgpu 的公开接口（[L59-L63](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L59-L63)）；而"显示服务器当前用哪个渲染节点"是纯 Linux 平台知识（dev_t、sysfs），只有 gpui_linux 知道怎么获取。类型定义在消费方、数据生产在平台方，依赖方向保持 `gpui_linux → gpui_wgpu`，不会反向（对照 u1-l1 的依赖图）。

**练习 3**：如果某台机器上 sysfs 信息缺失导致 `compositor_gpu_hint_from_dev_t` 返回 `None`，选卡会退化成什么行为？

**参考答案**：`compositor_match` 一级对全体适配器恒为 1（[src/wgpu_context.rs:L363-L372](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L363-L372) 的 match 落到 `_ => 1`），排序退化为三级：设备类型 → 后端，即独显优先、Vulkan 优先。兼容性兜底仍然由逐个实测保证——排序变"盲"了，但不会选到不能用的卡。

## 5. 综合实践

把本讲内容串成一个小任务：**用纯 Rust 复刻排序闭包，做一个"选卡模拟器"**（无需 GPU，任何机器可跑）。

1. **任务说明**：新建一个临时 Rust 工程（独立于 Zed 仓库，例如 `cargo new adapter-sort-sim`），把 4.2.4 的 5 个假设适配器编码成结构体数组，照抄 [src/wgpu_context.rs:L351-L397](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L351-L397) 的排序逻辑（用普通枚举模拟 `wgpu::DeviceType`/`Backend`，不引入 wgpu 依赖），打印排序前后的顺序。
2. **参考框架**（**示例代码**，仅演示排序逻辑，非项目原有代码）：

```rust
#[derive(Clone, Copy, PartialEq)]
enum DeviceType { DiscreteGpu, IntegratedGpu, Other, VirtualGpu, Cpu }

#[derive(Clone, Copy, PartialEq)]
enum Backend { Vulkan, Metal, Dx12, Gl }

struct AdapterInfo {
    name: &'static str,
    vendor: u32,
    device: u32,
    backend: Backend,
    device_type: DeviceType,
}

fn score(info: &AdapterInfo, device_id_filter: Option<u32>,
         compositor_gpu: Option<(u32, u32)>) -> (u8, u8, u8, u8) {
    let device_known = info.device != 0;
    let user_override: u8 = match device_id_filter {
        Some(id) if device_known && info.device == id => 0,
        _ => 1,
    };
    let compositor_match: u8 = match compositor_gpu {
        Some((vendor, device)) if device_known
            && info.vendor == vendor && info.device == device => 0,
        _ => 1,
    };
    let type_priority: u8 = match info.device_type {
        DeviceType::DiscreteGpu => 0,
        DeviceType::IntegratedGpu => 1,
        DeviceType::Other => 2,
        DeviceType::VirtualGpu => 3,
        DeviceType::Cpu => 4,
    };
    let backend_priority: u8 = match info.backend {
        Backend::Vulkan | Backend::Metal | Backend::Dx12 => 0,
        _ => 1,
    };
    (user_override, compositor_match, type_priority, backend_priority)
}

fn main() {
    let adapters = [
        AdapterInfo { name: "NVIDIA RTX 4060",     vendor: 0x10de, device: 0x2404, backend: Backend::Vulkan, device_type: DeviceType::DiscreteGpu },
        AdapterInfo { name: "Intel Iris Xe",       vendor: 0x8086, device: 0x46a6, backend: Backend::Vulkan, device_type: DeviceType::IntegratedGpu },
        AdapterInfo { name: "AMD RX 7600",         vendor: 0x1002, device: 0x7480, backend: Backend::Vulkan, device_type: DeviceType::DiscreteGpu },
        AdapterInfo { name: "llvmpipe",            vendor: 0xffff, device: 0x0000, backend: Backend::Vulkan, device_type: DeviceType::Cpu },
        AdapterInfo { name: "NVIDIA RTX 4060 GL",  vendor: 0x10de, device: 0x0000, backend: Backend::Gl,     device_type: DeviceType::Other },
    ];
    let compositor_gpu = Some((0x8086u32, 0x46a6u32));

    for (label, filter) in [("默认", None), ("ZED_DEVICE_ID=0x2404", Some(0x2404))] {
        let mut sorted = adapters.clone().to_vec();
        sorted.sort_by_key(|a| score(&a, filter, compositor_gpu));
        println!("== {label} ==");
        for a in &sorted {
            println!("  {:?}  score={:?}", a.name, score(a, filter, compositor_gpu));
        }
    }
}
```

3. **需要观察的现象**：两组输出顺序分别应为 `B → A → C → E → D` 与 `A → B → C → E → D`（对照 4.2.5 练习 1）。
4. **预期结果与核对点**：
   - 修改 `compositor_gpu` 为 `None`，观察核显 B 是否掉到 A、C 之后（退化为类型优先）；
   - 把 E 的 `device` 改成 `0x2404`，再设 `ZED_DEVICE_ID=0x2404`，观察 A、E 并列命中第一级后由稳定排序决定先后；
   - 把 D 的 `device_type` 改成 `DiscreteGpu`，观察它在没有 `reject_software` 语义参与时会不会"骗"到前排——进而体会 [L417-L424](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_wgpu/src/wgpu_context.rs#L417-L424) 软件渲染器跳过逻辑为何只信任 `device_type` 一层是不够的、实测环节为什么不可省。
5. **延伸（可选，待本地验证）**：在有真实 GPU 的 Linux 机器上运行 Zed 并对照其日志中的 `Found {} GPU adapter(s)` 列表，与你的模拟器输出互相印证。

## 6. 本讲小结

- `select_adapter_and_device` 的三段式策略：**全量枚举 → 四级键排序 → 按序逐个实测**，排序表达偏好，实测裁决兼容性，二者缺一不可。
- 四级优先键从高到低：`ZED_DEVICE_ID` 用户覆盖 > `CompositorGpuHint` 合成器所在卡 > 设备类型（Discrete > Integrated > Other > Virtual > Cpu）> 后端（Vulkan/Metal/Dx12 优于 GL 等）；键是四元组、字典序比较、稳定排序。
- 混合 GPU 假阳性是本设计的根本动因：`get_capabilities()` 可能谎报兼容（NVIDIA 声称支持 Vulkan Wayland 但合成器在 Intel 卡上），`try_adapter_with_surface` 用 64×64 最小配置 + 验证 error scope 真配一次来戳穿它，且测试中创建的 device/queue 直接复用、零浪费。
- `device_known`（`info.device != 0`）是前两级的前提：OpenGL 后端给所有适配器上报 `device=0`，因此 `ZED_DEVICE_ID` 对纯 GL 适配器**静默无效**。
- `ZED_DEVICE_ID` 的格式是可选 `0x` 前缀 + 恰好 4 个十六进制字符；非法值只记日志、降级为未设置，不崩溃。
- `CompositorGpuHint` 类型定义在 gpui_wgpu（消费方），数据生产（dev_t → sysfs → PCI ID）在 gpui_linux（平台方），保持了正确的依赖方向。

## 7. 下一步学习建议

本讲结束了对原生初始化中"选卡"环节的分析，第二单元还剩两讲，建议按序继续：

1. **u2-l3（Web 平台初始化）**：看同一问题的另一套解法——浏览器里没有"枚举全部适配器"的自由，`new_web_with_backend` 只能靠 `WebBackendPreference` + WebGPU 探测来在后端间做有限选择，与本讲的"全量枚举 + 实测"形成鲜明对照。
2. **u2-l4（设备丢失检测）**：本讲多次出现却未展开的 `check_compatible_with_surface`（轻查）与设备丢失标志将在那里展开；同时你会看到 `device_lost` 回调如何与多窗口共享上下文协作。
3. **源码延伸阅读**：把 [crates/gpui_linux/src/linux/wayland/client.rs](https://github.com/zed-industries/zed/blob/fa00dccc42311f8dc71c533105488b0dbd518138/crates/gpui_linux/src/linux/wayland/client.rs#L1287-L1303) 中 `detect_compositor_gpu` 的调用处读一遍（L1287-L1303），看清 Wayland 侧是如何拿到渲染节点的 `dev_t` 再调用 `compositor_gpu_hint_from_dev_t` 的，为 u6-l3 的平台集成专题做铺垫。
