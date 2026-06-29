# multi_usrp 高层 API

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `multi_usrp` 这一层存在的目的：它是一个「易用封装层」，把底层 `device` 与 `property_tree` 包成一套面向收发场景的便利 API。
- 读懂 `subdev_spec_t`（子设备规格）的 markup 字符串语法，并掌握单板多通道、多板多通道两种场景下的「全局通道号」映射规则。
- 看懂 `multi_usrp::make` 的工厂流程，以及为什么 RFNoC 设备会走一条不同的实现分支。
- 理解 `multi_usrp_impl` 中几乎每一个方法都是「把 `(通道, 主板)` 翻译成属性树路径再读写」的翻译层，并能据此推断任意一个 `set_xxx / get_xxx` 在源码里的实现位置。
- 知道多板、多通道使用时对「采样率、子设备规格长度、时间同步」的硬性约束。

本讲承接 u2-l1（设备发现与工厂模式）与 u2-l2（`device_addr_t`）。`device::make` 会返回一个抽象 `device`，本讲要讲的 `multi_usrp::make` 就是把它进一步包装成日常写收发程序时最常用的「黑盒」。

## 2. 前置知识

在进入源码前，先统一几个名词：

- **主板（motherboard，缩写 mboard）**：一块 USRP 物理板子。多块板子可以组网使用，索引从 0 开始记为 `m0, m1, ...`。
- **子板（daughterboard / dboard）**：插在主板上的射频前端模块，对应主板上的一个「槽位（slot）」，槽位名通常是 `A`、`B` 等。
- **子设备（subdevice / subdev）**：一块子板上的一个独立射频通路名，比如 `A`、`B`、`AB`。一块 TwinRX 子板可以有 `A`、`B` 两个独立接收通路。
- **通道（channel）**：应用程序视角下的一条数据流。`multi_usrp` 把「主板 × 子设备」摊平成一个**全局通道号** `chan = 0,1,2,...`。
- **属性树（property_tree）**：设备内部用一棵文件系统式的树来存放所有可配置状态（频率、增益、采样率……）。这是 u2-l4 的主题，本讲只需要知道「读写属性树节点 = 配置设备」。
- **RFNoC**：UHD 的现代设备架构（见第三单元）。`multi_usrp` 同时支持老设备和 RFNoC 设备，但 RFNoC 设备内部用的是另一套实现。

一句话直觉：`multi_usrp` 之于 `device`，就像一个「遥控器」之于「家电内部的一堆电路」。你不必关心属性树路径长什么样，只要按通道号按按钮即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `host/include/uhd/usrp/multi_usrp.hpp` | `multi_usrp` 抽象类的公共头文件，定义了几乎所有面向用户的 API、通配符常量与类级文档（含多板通道映射示例）。 |
| `host/lib/usrp/multi_usrp.cpp` | `multi_usrp_impl` 具体实现 + `make` 工厂函数 + 通道映射算法（`rx_chan_to_mcp` 等）+ tune 辅助函数。 |
| `host/include/uhd/usrp/subdev_spec.hpp` | `subdev_spec_pair_t` 与 `subdev_spec_t` 的定义，说明子设备规格 markup 字符串的语法。 |
| `host/examples/rx_multi_samples.cpp` | 多通道接收示例，演示「先选 subdev，再配通道」的标准用法。 |

## 4. 核心概念与源码讲解

### 4.1 multi_usrp 的定位：易用封装层

#### 4.1.1 概念说明

`uhd::device`（u2-l1 讲过）是所有设备的抽象基类，但它本身不提供「设置接收频率」「设置增益」这类高频度接口——它把所有状态都藏在 `property_tree` 里，你要自己拼路径去读写。这对应用开发者太低层了。

`multi_usrp` 就是为此而生的**易用封装层（facade）**。它持有两个东西：

1. 一个 `device::sptr`（底层设备对象）；
2. 一个 `property_tree::sptr`（从设备取出的属性树引用）。

然后把「设置 RX 频率」「获取主板数」「发起流命令」等上百个高频接口，统一翻译成对属性树的读写。类的文档注释里写得非常直白：

> This class facilitates ease-of-use for most use-case scenarios. The wrapper provides convenience functions to tune the devices, set the dboard gains, antennas, filters, and other properties.

[multi_usrp.hpp:49-58](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L49-L58)：类级文档，说明这一层是「为绝大多数用例提供便利」的封装层。

理解了「封装层」这一定位，就能解释 `multi_usrp` 几乎所有设计选择：所有方法都带可选的 `chan` 或 `mboard` 参数；大部分参数有「单板/单通道时可不填」的默认值；有一组 `ALL_*` 通配符常量用来「应用到全部」。

#### 4.1.2 核心流程

一个 `multi_usrp` 对象的生命周期可以概括为三步：

```text
1. multi_usrp::make(dev_addr)
       └─ 内部调 device::make(dev_addr, device::USRP) 打开设备
       └─ 构造 multi_usrp_impl，构造时从设备取出 property_tree
2. 配置阶段：usrp->set_rx_subdev_spec / set_rx_rate / set_rx_freq ...
       └─ 每个方法把 (chan, mboard) 翻译成属性树路径，再 set/get
3. 收发阶段：usrp->get_rx_stream(args) / get_tx_stream(args)
       └─ 委托回底层 device::get_rx_stream，拿到流器后开始 recv/send
```

注意第 3 步：流式传输相关的真正功能**不在 `multi_usrp` 里**，而在 streamer 对象里。`multi_usrp::get_rx_stream` 只是一个转发便捷方法（流式 API 是 u2-l5 的主题）。

#### 4.1.3 源码精读

**通配符常量**——这是理解整个 API「应用到全部」语义的钥匙：

[multi_usrp.hpp:102-112](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L102-L112)：声明四个 `ALL_*` 常量。

它们的具体值定义在 .cpp 文件开头：

[multi_usrp.cpp:47-50](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L47-L50)：`ALL_MBOARDS` 与 `ALL_CHANS` 都是 `size_t(~0)`（即全 1，一个非常大的数）；`ALL_GAINS = ""`；`ALL_LOS = "all"`。

```cpp
const size_t multi_usrp::ALL_MBOARDS    = size_t(~0);
const size_t multi_usrp::ALL_CHANS      = size_t(~0);
const std::string multi_usrp::ALL_GAINS = "";
const std::string multi_usrp::ALL_LOS   = "all";
```

这解释了后面会反复出现的模式：「如果参数等于 `ALL_MBOARDS`，就遍历所有主板分别执行；否则只对指定主板执行」。

**工厂方法**：

[multi_usrp.hpp:121-121](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L121-L121)：`static sptr make(const device_addr_t& dev_addr);`，文档注明找不到设备抛 `uhd::key_error`，找到的设备数少于预期抛 `uhd::index_error`。

**通往底层的三个桥接方法**：

[multi_usrp.hpp:137-147](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L137-L147)：`get_device()` 返回底层 `device`、`get_tree()` 返回 `property_tree`、`get_rx_stream/get_tx_stream` 转发到底层流器。

`get_device()` 的文档有一段值得注意的警告：

[multi_usrp.hpp:123-137](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L123-L137)：明确「不推荐」直接用 `get_device()`；对 RFNoC 设备它返回的不是「真正的 `uhd::device`」，功能受限。这进一步印证 `multi_usrp` 是面向用户的「正门」，绕过它走后门会有坑。

#### 4.1.4 代码实践

1. **实践目标**：不读实现，只读头文件的「类级文档 + 方法分组注释」，建立对 `multi_usrp` API 全貌的直觉。
2. **操作步骤**：
   - 打开 `host/include/uhd/usrp/multi_usrp.hpp`，只看带 `/**********/` 的分节注释（如 `Mboard methods`、`RX methods`、`TX methods`、`GPIO methods`、`Filter API methods`）。
   - 数一数它分成几个功能大块。
3. **需要观察的现象**：你会看到 API 是「对称」的——RX 与 TX 各有一套几乎一一对应的 `set_rx_* / set_tx_*`（rate、freq、gain、antenna、bandwidth、dc_offset、iq_balance……）。
4. **预期结果**：能列出至少 6 个成对出现的 RX/TX 方法名，并说出它们各自属于「主板级」还是「通道级」。
5. 待本地验证（无硬件也可完成，纯文档阅读）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `multi_usrp` 的几乎所有方法都带一个可选的 `chan` 或 `mboard` 参数，且大多数默认值是 `0`？

> **参考答案**：因为 `multi_usrp` 设计上要同时覆盖「单板单通道」和「多板多通道」两种场景。单板单通道时，`chan=0`/`mboard=0` 的默认值让你完全不必关心索引；多板多通道时再显式传入索引。这正是「易用封装层」对最常见用例做特殊优化的体现。

**练习 2**：`get_device()` 的文档为什么不推荐使用？

> **参考答案**：`multi_usrp` 提供了更高层、更安全的 API；直接拿 `device` 绕过它会失去封装的保护。尤其对 RFNoC 设备，`get_device()` 返回的对象直接设备访问被锁住，功能受限。需要底层能力时应优先用 `get_tree()`、`get_radio_control()` 等有明确语义的入口。

---

### 4.2 subdev_spec：子设备规格与通道映射规则

#### 4.2.1 概念说明

`multi_usrp` 把「主板 × 子设备」摊平成全局通道号 `0,1,2,...`。那它**怎么知道**通道 0 对应哪块板的哪个射频通路？答案就是 **子设备规格（subdev spec）**：一张有序列表，告诉驱动「这块主板上的哪些子设备，按什么顺序，映射成本板的本地通道」。

子设备规格由两个类型构成（定义在 `subdev_spec.hpp`）：

- `subdev_spec_pair_t`：一对名字 `(db_name, sd_name)`，即「槽位名 : 子设备名」，例如 `("A", "A")` 写成 markup 就是 `"A:A"`。
- `subdev_spec_t`：继承自 `std::vector<subdev_spec_pair_t>`，表示「一块主板的子设备规格列表」。可以从一个 markup 字符串构造。

markup 字符串的规则（来自源码注释）：**用空白分隔的一串 `dboard:subdev` 对，第 1 对 = 本板本地通道 0，第 2 对 = 本板本地通道 1，依此类推。**

#### 4.2.2 核心流程：全局通道号如何映射回「主板 + 本地通道」

类文档把通道映射规则分两种场景讲清楚：

[multi_usrp.hpp:60-72](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L60-L72)：单设备多通道时通道由前端规格决定、且所有通道共享同一个 RX/TX 采样率；多设备时通道由设备地址参数决定、所有主板共享同一采样率、同一前端规格**长度**，且必须时间同步。

把映射规则形式化。设第 \(m\) 块主板的子设备规格长度为 \(s_m\)（即 `get_rx_subdev_spec(m).size()`），全局通道号 `chan` 落在第 \(m\) 块板上，满足：

\[
\sum_{i=0}^{m-1} s_i \;\le\; chan \;<\; \sum_{i=0}^{m} s_i
\]

而它在该板内的**本地通道号**为：

\[
local = chan - \sum_{i=0}^{m-1} s_i
\]

这正是 .cpp 里 `rx_chan_to_mcp` 的算法（下面会贴）。它解释了几个硬性约束的由来：

- **所有主板必须共享同一前端规格长度 \(s_m\)**：否则不同板摊出的通道数不一致，全局编号无法整齐排列。
- **所有板共享同一采样率**：因为流器会把多通道数据交织在一起，采样率必须一致才能对齐。
- **多板必须时间同步**：多板数据要在时间轴上对齐才能拼成相干阵列，这就引出 4.4 节的同步要求。

#### 4.2.3 源码精读

**markup 字符串与结构定义**：

[subdev_spec.hpp:59-71](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/subdev_spec.hpp#L59-L71)：`subdev_spec_t` 继承 `std::vector<subdev_spec_pair_t>`，可由 markup 字符串构造；注释说明「第一个 pair = 通道 0，第二个 = 通道 1」。

[subdev_spec.hpp:20-45](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/subdev_spec.hpp#L20-L45)：`subdev_spec_pair_t` 的 `db_name`/`sd_name` 两个字段，以及 `to_string()` 返回 `"db:sd"`。

**类文档里的多板通道映射示例（本讲最重要的代码片段之一）**：

[multi_usrp.hpp:73-93](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L73-L93)：两块板、两路 RX 的最小配置示例。

```cpp
//create a multi_usrp with two boards in the configuration
device_addr_t dev_addr;
dev_addr["addr0"] = "192.168.10.2"
dev_addr["addr1"] = "192.168.10.3";
multi_usrp::sptr dev = multi_usrp::make(dev_addr);

//set the board on 10.2 to use the A RX frontend (RX channel 0)
dev->set_rx_subdev_spec("A:A", 0);

//set the board on 10.3 to use the B RX frontend (RX channel 1)
dev->set_rx_subdev_spec("A:B", 1);

//set both boards to use the AB TX frontend (TX channels 0 and 1)
dev->set_tx_subdev_spec("A:AB", multi_usrp::ALL_MBOARDS);
```

读这段示例要注意三点：

1. 两块板用 `addr0`/`addr1` 在**同一个 `device_addr_t`** 里给出（u2-l2 讲过的多设备地址写法），`make` 一次返回覆盖两块板的对象。
2. `set_rx_subdev_spec("A:A", 0)` 给主板 0 指定 1 个 RX 前端 → 贡献全局通道 0；`set_rx_subdev_spec("A:B", 1)` 给主板 1 指定 1 个 RX 前端 → 贡献全局通道 1。两板各 1 个，规格长度一致（满足约束）。
3. 第二个参数是**主板索引**（不是通道），用 `ALL_MBOARDS` 表示「所有板都用这个规格」。

**`set_rx_subdev_spec` 的实现——就是往属性树写一个节点**：

[multi_usrp.cpp:864-873](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L864-L873)：把 spec 写到 `/mboards/<m>/rx_subdev_spec`，`ALL_MBOARDS` 时遍历所有主板。

```cpp
void set_rx_subdev_spec(const subdev_spec_t& spec, size_t mboard) override
{
    if (mboard != ALL_MBOARDS) {
        _tree->access<subdev_spec_t>(mb_root(mboard) / "rx_subdev_spec").set(spec);
        return;
    }
    for (size_t m = 0; m < get_num_mboards(); m++) {
        set_rx_subdev_spec(spec, m);
    }
}
```

这就是 4.1 节说的「翻译层」模式的最典型体现：**用户 API → 属性树路径**。

**`get_rx_subdev_spec` 的「自动默认」行为**：

[multi_usrp.cpp:875-899](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L875-L899)：如果当前 spec 为空，自动取「第一块子板的第一个前端」作为默认规格并写回树。

这个自动默认解释了：**为什么单板单通道程序常常不必显式调用 `set_rx_subdev_spec`**——第一次 `get_rx_subdev_spec` 时它会被自动填上。

**通道映射核心算法 `rx_chan_to_mcp`**：

[multi_usrp.cpp:2494-2511](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L2494-L2511)：把全局通道号拆成 `(mboard, local_chan)`，正是上面公式 4.2.2 的逐字段实现。

```cpp
mboard_chan_pair rx_chan_to_mcp(size_t chan)
{
    mboard_chan_pair mcp;
    mcp.chan = chan;
    for (mcp.mboard = 0; mcp.mboard < get_num_mboards(); mcp.mboard++) {
        size_t sss = get_rx_subdev_spec(mcp.mboard).size();
        if (mcp.chan < sss)
            break;
        mcp.chan -= sss;          // 减去当前板贡献的通道数
    }
    if (mcp.mboard >= get_num_mboards()) {
        throw uhd::index_error(...);   // 全局通道号越界
    }
    return mcp;
}
```

> `mcp` = mboard-channel pair。`(...)` 处省略了原代码里格式化的错误信息字符串，其余与仓库实现一致。

**总通道数 = 各板规格长度之和**：

[multi_usrp.cpp:901-908](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L901-L908)：`get_rx_num_channels` 就是把每块板的 `get_rx_subdev_spec(m).size()` 加起来。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：对照 `multi_usrp.hpp` 的类文档示例，写出「两块板、2 通道接收」的最小配置伪代码，并能预测每个全局通道号落在哪块板。
2. **操作步骤**：
   - 阅读 [multi_usrp.hpp:73-93](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L73-L93) 的示例注释。
   - 自己写一段伪代码：构造两块板的 `device_addr_t` → `make` → 分别给主板 0、1 设 RX 规格 → 设公共采样率 → 取 2 通道流器。
3. **需要观察的现象**：写出后，用 4.2.2 的公式手算：若主板 0 规格 `"A:A"`、主板 1 规格 `"A:B"`，全局通道 0 和 1 分别落在哪块板、本地通道是多少？
4. **预期结果**：通道 0 → 主板 0 / 本地 0；通道 1 → 主板 1 / 本地 0（因为每块板都只贡献 1 个通道）。
5. **参考伪代码（示例代码，非项目原文件）**：

   ```cpp
   // 示例代码：两块板、2 通道接收的最小配置
   uhd::device_addr_t dev_addr;
   dev_addr["addr0"] = "192.168.10.2";
   dev_addr["addr1"] = "192.168.10.3";
   auto usrp = uhd::usrp::multi_usrp::make(dev_addr);

   // 先选 subdev —— 通道映射会据此生效（见 rx_multi_samples.cpp 的注释）
   usrp->set_rx_subdev_spec("A:A", 0); // 主板 0 → 全局通道 0
   usrp->set_rx_subdev_spec("A:B", 1); // 主板 1 → 全局通道 1

   // 所有板共享同一采样率（多板硬性约束）
   usrp->set_rx_rate(1e6);

   // 取 2 通道流器：channels = {0, 1}
   uhd::stream_args_t stream_args("fc32");
   stream_args.channels = {0, 1};
   auto rx_stream = usrp->get_rx_stream(stream_args);
   ```

   并对照真实示例 [rx_multi_samples.cpp:137-189](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_multi_samples.cpp#L137-L189)：注意它有一行关键注释 `// always select the subdevice first, the channel mapping affects the other settings`，以及随后用 `stream_args.channels = channel_nums` 把全局通道号喂给流器。

#### 4.2.5 小练习与答案

**练习 1**：如果一块主板的子设备规格写成 `"A:A B:B"`，它会给全局通道贡献几个本地通道？分别是哪两个 `(db, sd)` 对？

> **参考答案**：贡献 2 个本地通道。markup 字符串以空白分隔，第一对 `"A:A"` = 本地通道 0（槽位 A、子设备 A），第二对 `"B:B"` = 本地通道 1（槽位 B、子设备 B）。

**练习 2**：为什么类文档要求「所有主板必须共享同一前端规格**长度**」，而不是要求规格**完全相同**？

> **参考答案**：全局通道号是按「各板规格长度依次累加」编号的，只要每块板贡献的通道数相同，全局编号就能整齐排列。至于每块板具体用哪个子设备（`A:A` 还是 `A:B`），允许不同——这正是上面示例里主板 0 用 `A:A`、主板 1 用 `A:B` 的合法用法。

---

### 4.3 multi_usrp 实现：property_tree 翻译层

#### 4.3.1 概念说明

4.1 节说 `multi_usrp` 是封装层，4.2 节已经看到 `set_rx_subdev_spec` 的实现就是「往属性树写一个节点」。本节把这个规律推广到**几乎所有方法**：

> `multi_usrp_impl` 的几乎每一个 `set_xxx / get_xxx`，都在做同一件事——把 `(chan, mboard)` 这种用户友好的参数，翻译成一条属性树路径，然后 `.set()` 或 `.get()`。

一旦掌握这条规律，你看到任何一个新方法（比如 `set_rx_bandwidth`），不用读实现也能猜到它落在属性树的哪个节点（多半是 `rx_rf_fe_root(chan) / "bandwidth" / "value"`）。这就把「记上百个 API」简化成「记一套路径命名约定」。

#### 4.3.2 核心流程：从通道号到属性树路径

路径翻译分两跳，由几个私有辅助函数完成：

```text
全局 chan
   │  rx_chan_to_mcp(chan)        →  (mboard, local_chan)   [拆板]
   ▼
subdev_spec_pair_t (db_name, sd_name)
   │  rx_rf_fe_root(chan)         →  /mboards/<m>/dboards/<db>/rx_frontends/<sd>   [射频前端]
   │  rx_dsp_root(chan)           →  /mboards/<m>/rx_dsps/<local_dsp>              [DSP 核]
   ▼
_tree->access<T>( path / "xxx" / "value" ).set(...)   [最终读写]
```

`ALL_MBOARDS` / `ALL_CHANS` 的处理则统一遵循一个模式：「若是通配符就 for 循环遍历所有目标，否则只作用于单个目标」。这个模式在 `set_master_clock_rate`、`set_rx_rate`、`set_rx_subdev_spec`、`set_time_now` 等几十个方法里重复出现。

#### 4.3.3 源码精读

**实现类与构造**：

[multi_usrp.cpp:275-291](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L275-L291)：`multi_usrp_impl` 持有 `_dev` 与 `_tree`，构造函数仅做 `_tree = _dev->get_tree();`——封装层的「取树」动作就发生在这一行。

```cpp
class multi_usrp_impl : public multi_usrp
{
public:
    multi_usrp_impl(device::sptr dev) : _dev(dev)
    {
        _tree = _dev->get_tree();
    }
    ...
private:
    device::sptr _dev;
    property_tree::sptr _tree;
```

**`make` 工厂：老设备与 RFNoC 设备的分叉**：

[multi_usrp.cpp:2767-2779](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L2767-L279):工厂实现。

```cpp
multi_usrp::sptr multi_usrp::make(const device_addr_t& dev_addr)
{
    device::sptr dev = device::make(dev_addr, device::USRP);   // 复用 u2-l1 的设备工厂
    auto rfnoc_dev = std::dynamic_pointer_cast<rfnoc::detail::rfnoc_device>(dev);
    if (rfnoc_dev) {
        return rfnoc::detail::make_rfnoc_device(rfnoc_dev, dev_addr);  // RFNoC 分支
    }
    return std::make_shared<multi_usrp_impl>(dev);             // 老设备分支
}
```

要点：

- 第一行 `device::make(dev_addr, device::USRP)` 复用了 u2-l1 讲的设备工厂，`device::USRP` 就是 u2-l1 里的 `device_filter_t` 过滤器。
- 用 `dynamic_pointer_cast` 判断返回的是不是 RFNoC 设备。**如果是**，就交给 `make_rfnoc_device` 构造一个**另一套实现**（RFNoC 设备的 `multi_usrp` 实现在别处，它内部用 `rfnoc_graph` + `mb_controller`，而不是直接读写这棵老属性树）。
- **如果不是**（老设备），才构造本文件里的 `multi_usrp_impl`。

这正是 4.1.3 里 `get_device()` 文档说「RFNoC 设备返回的不是真正的 `uhd::device`」的根因：RFNoC 设备走的是另一条实现链路。这一点会在第三单元 RFNoC 架构里展开。

**翻译层的三种典型范例**：

范例 1——「主板级」属性（不涉及通道）：[multi_usrp.cpp:791-794](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L791-L794)：`get_num_mboards` 就是 `_tree->list("/mboards").size()`。

```cpp
size_t get_num_mboards(void) override
{
    return _tree->list("/mboards").size();
}
```

范例 2——「DSP 核」属性（采样率）：[multi_usrp.cpp:920-930](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L920-L930)：`set_rx_rate` 写 `rx_dsp_root(chan) / "rate" / "value"`，并附带一个「请求率与实际率相差太大就告警」的提示。

```cpp
void set_rx_rate(double rate, size_t chan) override
{
    if (chan != ALL_CHANS) {
        _tree->access<double>(rx_dsp_root(chan) / "rate" / "value").set(rate);
        do_samp_rate_warning_message(rate, get_rx_rate(chan), "RX");
        return;
    }
    for (size_t c = 0; c < get_rx_num_channels(); c++) {
        set_rx_rate(rate, c);   // ALL_CHANS：逐通道应用
    }
}
```

范例 3——「射频前端」属性（频率）：[multi_usrp.cpp:947-975](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L947-L975)：`set_rx_freq` 把请求交给辅助函数 `tune_xx_subdev_and_dsp`，后者把「目标中心频率」拆成「射频前端频率 + DSP（CORDIC）频率」两部分分别写入对应子树。

**路径翻译辅助函数**：

[multi_usrp.cpp:2648-2660](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L2648-L2660)：`rx_rf_fe_root(chan)` 先用 `rx_chan_to_mcp` 拆出 `(mboard, local_chan)`，再读该板的 subdev_spec 取出 `(db_name, sd_name)`，拼出 `/mboards/<m>/dboards/<db>/rx_frontends/<sd>`。

[multi_usrp.cpp:2548-2575](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L2548-L2575)：`rx_dsp_root(chan)` 类似，但用到一个可选的 `rx_chan_dsp_mapping` 节点做本地 DSP 核重映射，最终拼出 `/mboards/<m>/rx_dsps/<local_dsp>`。

注意 `rx_rf_fe_root` 的关键一步是 `get_rx_subdev_spec(mcp.mboard).at(mcp.chan)`——**subdev_spec 既是用户配置，也是路径生成的依据**。这把 4.2 节（规格）与本节（翻译层）牢牢扣在一起：改了 subdev_spec，所有 `rx_rf_fe_root` 计算出的路径都会变，所以示例注释才强调「always select the subdevice first」。

#### 4.3.4 代码实践

1. **实践目标**：用「翻译层」规律，**不读实现**就预测两个 API 的属性树路径，再到源码里验证。
2. **操作步骤**：
   - 凭直觉预测 `set_rx_antenna(ant, chan)` 和 `get_rx_gain(name, chan)` 分别读写哪条属性树路径。
   - 在 `multi_usrp.cpp` 里搜索这两个方法的实现，核对路径。
3. **需要观察的现象**：路径应该都以 `rx_rf_fe_root(chan)` 起头，并带一个 `value` 叶子。
4. **预期结果**：
   - `set_rx_antenna` → [multi_usrp.cpp:1644-1647](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L1644-L1647)：`rx_rf_fe_root(chan) / "antenna" / "value"`。
   - `get_rx_gain` → [multi_usrp.cpp:1565-1572](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L1565-L1572)：通过 `rx_gain_group(chan)->get_value(name)`（增益因涉及多个增益元件，用了 gain_group 聚合，而非单条路径）。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`multi_usrp::make` 里为什么要做一次 `dynamic_pointer_cast<rfnoc::detail::rfnoc_device>`？

> **参考答案**：因为 `device::make` 返回的是抽象 `device`，但现代 RFNoC 设备与老设备的 `multi_usrp` 实现**完全不同**。这次转型是一个运行时类型探测：探测到 RFNoC 设备就交给专门的 `make_rfnoc_device`（用 `rfnoc_graph`/`mb_controller` 那一套），否则才用本文件里直接读写属性树的 `multi_usrp_impl`。同一个公共头、两套实现，由转型来分派。

**练习 2**：为什么 `get_rx_gain` 不像 `set_rx_antenna` 那样直接读一条属性树路径，而要用 `rx_gain_group`？

> **参考答案**：因为一条射频链路上可能有**多个**增益元件（如 PGA + LNA），`get_rx_gain(ALL_GAINS, chan)` 要把它们**求和**，`get_rx_gain(name, chan)` 要按名字定位其中之一。`gain_group` 把 `rx_codecs`（ADC 增益）和射频前端的多个增益子树按优先级聚合起来，提供统一的 get/set/range 接口。这是 `multi_usrp` 在「纯翻译」之外，少数几处做了聚合逻辑的地方。

---

### 4.4 多板与多通道的约束：采样率与时间同步

#### 4.4.1 概念说明

`multi_usrp` 之所以叫 **multi**，是因为它能把多块板子当成一个对象来用。但「当成一个对象」是有代价的：多块板的数据要拼到一起，就必须满足一组**一致性约束**。类文档把这些约束列得很清楚（4.2.2 节引用过），归纳成三条：

1. **公共采样率**：所有通道（单板）或所有主板（多板）共享同一个 RX 采样率和同一个 TX 采样率。
2. **公共规格长度**：所有主板的 RX 子设备规格长度相同，TX 同理。
3. **时间同步**：多板使用时，所有主板必须先做时间同步（否则不同板的样本在时间轴上对不齐）。

第 3 条是新手最容易踩的坑，所以 `multi_usrp` 专门提供了一组时间 API。

#### 4.4.2 核心流程：时间同步两步法

多板时间同步最稳妥的入口是 `set_time_unknown_pps`。它的文档说这是一个「两步、最多耗时 2 秒」的过程：

[multi_usrp.hpp:302-322](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L302-L322)：`set_time_unknown_pps` 文档，说明在 PPS 沿未知时用它同步，分两步、最多 2 秒。

实现里的两步：

1. **捕捉 PPS 沿**：反复轮询 `get_time_last_pps()`，直到它的值发生跳变（说明刚来了一个 PPS 沿）。
2. **在下一个 PPS 同步设时间**：调用 `set_time_next_pps(time_spec, ALL_MBOARDS)`，让所有主板在**同一个** PPS 沿把时间寄存器置为指定值。

#### 4.4.3 源码精读

**两步法的实现**：

[multi_usrp.cpp:492-526](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L492-L526)：`set_time_unknown_pps` 实现。

```cpp
void set_time_unknown_pps(const time_spec_t& time_spec) override
{
    // Step1: 等待 last_pps 发生跳变，抓住 PPS 沿
    time_spec_t time_start_last_pps = get_time_last_pps();
    while (time_start_last_pps == get_time_last_pps()) {
        // 超过 1.1 秒还没跳变 → 抛错（可能没接 PPS 信号）
        ...
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    // Step2: 在下一个 PPS 同步设时间（所有主板）
    set_time_next_pps(time_spec, ALL_MBOARDS);
    std::this_thread::sleep_for(std::chrono::seconds(1));
    // 校验：各板时间差应在 RTT 量级（< 10 ms）
    for (size_t m = 1; m < get_num_mboards(); m++) {
        time_spec_t time_0 = this->get_time_now(0);
        time_spec_t time_i = this->get_time_now(m);
        if (time_i < time_0 or (time_i - time_0) > time_spec_t(0.01)) {
            UHD_LOGGER_WARNING("MULTI_USRP") << ...;   // 告警：板间时间偏差过大
        }
    }
}
```

要点解读：

- Step1 的超时上限（1.1 秒）是有道理的：PPS 每秒一拍，1.1 秒内必然该跳一次；不跳就是没信号。
- Step2 用 `ALL_MBOARDS`，保证所有主板在**同一个** PPS 沿改时间——这是多板对齐的关键。
- 结尾的校验循环用 10 ms 作为阈值：「大于 RTT 但不要太大」。控制包一来一回有网络时延（RTT），所以板间时间不可能完全相等，只要在 10 ms 内就算同步成功。

**同步状态查询**：

[multi_usrp.cpp:528-537](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L528-L537)：`get_time_synchronized` 用同样的 10 ms 阈值判断所有主板是否时间一致，返回布尔值。

[multi_usrp.hpp:324-330](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L324-L330)：对应声明，文档说明「检查所有时间寄存器大致接近但不精确（因为 RTT 会波动）」。

**真实示例里的同步用法**：

[rx_multi_samples.cpp:150-171](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_multi_samples.cpp#L150-L171)：示例给出三种时间初始化方式——`now`（非真同步）、`pps`（调 `set_time_source("external")` + `set_time_unknown_pps(0)`）、`mimo`（两板用 MIMO 线缆主从同步）。这段代码是多板同步的最佳阅读样板。

#### 4.4.4 代码实践

1. **实践目标**：阅读示例，理清「多板 MIMO 同步」的调用序列，画出状态流转。
2. **操作步骤**：
   - 读 [rx_multi_samples.cpp:159-171](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_multi_samples.cpp#L159-L171) 的 `mimo` 分支。
   - 列出它依次调用了哪些 `multi_usrp` 方法，以及每一步的目的。
3. **需要观察的现象**：序列里会出现「设主板的时钟源/时间源为 `mimo`」「给主板 0 设时间」「sleep 等待从板锁定」三类动作。
4. **预期结果**：得到一条 `set_clock_source("mimo", 1) → set_time_source("mimo", 1) → set_time_now(0, 0) → sleep` 的序列。其中 `1` 表示从板索引。
5. 待本地验证（无硬件时为源码阅读型实践，重点在理解序列而非运行）。

#### 4.4.5 小练习与答案

**练习 1**：`set_time_unknown_pps` 为什么用 10 ms 作为板间时间偏差的告警阈值？

> **参考答案**：多板通过控制包交互读时间，一来一回有网络往返时延（RTT），所以即使完全同步，读回来的时间也会有 RTT 量级的差。10 ms 远大于典型 RTT、又远小于一拍 PPS（1 s），是一个「大于 RTT 但不至于把 1 秒的整拍偏差误判为正常」的合理阈值。

**练习 2**：如果两块板的 RX 子设备规格长度不同（主板 0 给 2 个前端、主板 1 给 1 个前端），会发生什么？

> **参考答案**：全局通道号映射仍然按「各板规格长度累加」执行，但多板的一致性约束被破坏：流器会把多通道数据按「每板通道数相同」交织，规格长度不一致会导致通道对齐错乱、采样率无法统一。这也是类文档把「所有主板共享同一前端规格长度」列为硬性约束的原因。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「源码阅读 + 配置推演」的综合任务：

**任务**：假设你有一台双板 USRP（主板 0 = `192.168.10.2`，主板 1 = `192.168.10.3`），要做一个 **2 通道相干接收** 程序。请：

1. **写配置伪代码**（示例代码）：依次完成
   - 构造两板 `device_addr_t` 并 `multi_usrp::make`；
   - 给两板分别设 RX 子设备规格（主板 0 用 `A:A`，主板 1 用 `A:B`）；
   - 设公共 RX 采样率（如 1 MSps），并回读实际率；
   - 用 `set_time_unknown_pps` 做多板时间同步，并用 `get_time_synchronized` 校验；
   - 取 `channels = {0, 1}` 的流器。
2. **源码跟踪**：在你的伪代码每一步旁边，标注它最终会触发 `multi_usrp.cpp` 里的哪个方法、写到属性树的哪条路径（参考 4.3 的翻译层规律）。
3. **自检**：用 4.2.2 的公式手算「全局通道 0、1 各落在哪块板的哪个本地通道」，确认与你设的 subdev 一致。

**验收标准**：

- 伪代码顺序正确——尤其「先选 subdev，再做任何带通道号的配置」（对照 [rx_multi_samples.cpp:137-139](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_multi_samples.cpp#L137-L139) 的注释）。
- 路径标注能对应到 `rx_rf_fe_root` / `rx_dsp_root` / `mb_root` 三类前缀。
- 时间同步放在「配置射频参数之前或之后」的取舍能说清理由（提示：参考 [multi_usrp.hpp:291-294](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L291-L294) 关于「改时钟源会丢失已设时间」的注释）。

> 待本地验证：本任务以源码阅读与配置推演为主；若手边有真实双板硬件，可编译运行 [rx_multi_samples.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rx_multi_samples.cpp) 用 `--subdev`、`--channels "0,1"`、`--sync mimo` 等参数实测。

## 6. 本讲小结

- `multi_usrp` 是一层**易用封装（facade）**：它持有 `device` 与 `property_tree`，把高频收发配置包成带 `(chan, mboard)` 参数的便利 API；`make` 是入口，`get_device/get_tree/get_rx_stream` 是通往底层的桥。
- 四个通配符常量 `ALL_MBOARDS / ALL_CHANS / ALL_GAINS / ALL_LOS` 是「应用到全部」语义的钥匙，源码里统一用「若是通配符就遍历、否则单点」的模式处理。
- 子设备规格 `subdev_spec_t` 是一张有序列表，markup 字符串用空白分隔 `dboard:subdev` 对，决定每块板贡献哪些本地通道；全局通道号由各板规格长度依次累加得到。
- `multi_usrp_impl` 的几乎所有方法都是**属性树翻译层**：`chan → (mboard, local) → 路径 → _tree->access().set/get`。掌握 `rx_chan_to_mcp`、`rx_rf_fe_root`、`rx_dsp_root` 三个辅助函数即可举一反三。
- `make` 用 `dynamic_pointer_cast` 在「老设备 `multi_usrp_impl`」与「RFNoC 设备 `make_rfnoc_device`」之间分派——同一个公共头、两套实现。
- 多板多通道有三条硬性约束：公共采样率、公共规格长度、时间同步；`set_time_unknown_pps` 的两步法（抓 PPS 沿 + 下一个 PPS 同步设时）是最稳妥的多板对齐手段。

## 7. 下一步学习建议

- **紧接本讲**：学 u2-l4「属性树 property_tree 机制」。本讲反复出现的 `_tree->access<T>(path)`、`rx_rf_fe_root`、`mb_root` 路径，其底层容器就是 property_tree，搞懂它能让你真正「看进」`multi_usrp_impl` 的每一行。
- **流式收发**：学 u2-l5「流式 API：stream_args_t 与收发流器」和 u2-l6「接收流与元数据」。本讲的 `get_rx_stream` 只是转发，真正的 `recv` 循环、`rx_metadata_t` 标志在那两讲。
- **现代设备主线**：本讲提到 RFNoC 设备走 `make_rfnoc_device` 另一套实现。要理解那条线，进入第三单元 u3-l1「RFNoC 架构与 rfnoc_graph 会话」，对照本讲的 `multi_usrp` 理解「高层封装」在 RFNoC 时代如何演进。
- **源码延伸阅读**：直接通读 [multi_usrp.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp) 的 tune 辅助函数 `tune_xx_subdev_and_dsp`（第 121–258 行），理解 `set_rx_freq` 如何把一个目标频率拆成 RF + DSP 两段，是进阶到 RFNoC radio/experts 属性传播的前置知识。
