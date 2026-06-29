# dboard 子板与前端抽象

## 1. 本讲目标

本讲深入 USRP 的「射频前端」一层。学完后你应当掌握：

- **dboard_manager** 是什么：它如何用一个自注册的注册表，把物理子板（daughterboard）映射成属性树上的 `rx_frontends` / `tx_frontends` 节点，并管理子板的整个生命周期。
- **fe_connection** 是什么：它如何用一个 4 字符字符串（如 `IQ` / `QI` / `I`）精确描述射频前端到 FPGA 的「I/Q 接线方式」（交换、取反、实采/正交/外差）。
- **cores** 是什么：`host/lib/usrp/cores/` 下的 DSP 核（尤其 `rx_dsp_core_3000`）如何消费 `fe_connection`、做数字下变频（DDC）、抽取，并补偿 CIC 的算法增益。

这三个最小模块串起来，回答了一个核心问题：**当一块子板插到主板上时，软件是如何「认出它、描述它、驱动它」的。**

## 2. 前置知识

在开始前，你需要先建立以下概念（前序讲义已覆盖，这里只做一句话回顾）：

- **主板（mboard）与子板（dboard）**：USRP 的硬件分两层。主板提供时钟、网络、FPGA；子板是插在主板上的射频前端（含 ADC/DAC、混频器、滤波器）。一个主板插槽上插一块子板。
- **property_tree（属性树）**：UHD 用一棵类似文件系统的树来保存所有设备配置（见 u2-l4）。子板的配置就挂在 `mboards/0/dboards/A/rx_frontends/...` 这样的路径下。
- **desired / coerced 双值模型**：属性树节点有「期望值」和「强制值」之分，读回的往往是 coerced。
- **multi_usrp / subdev_spec**：高层 API 用 `A:0` 这样的子设备规格字符串选中某个前端（见 u2-l3）。
- **register / factory 自注册**：设备与块都靠 `UHD_STATIC_BLOCK` 在 `main` 之前把自己登记进全局表（见 u2-l1、u3-l2）。

本讲出现的几个新术语：

| 术语 | 含义 |
| --- | --- |
| dboard（daughterboard） | 子板，射频前端硬件 |
| subdev | 子设备，一块子板上的一个独立通道（如双通道子板有两个 subdev） |
| dboard_id | 子板身份号，一个 16 位整数，烧在子板 EEPROM 里 |
| dboard_iface | 子板访问主板资源的接口（GPIO/SPI/I2C/时钟/前端连接） |
| fe_connection | 射频前端到 FPGA 的 I/Q 接线描述 |
| DSP core | FPGA 里的一段数字信号处理逻辑，由主机侧 C++ 类驱动（poke 寄存器） |
| DDC / CIC / halfband | 数字下变频 / 级联积分梳状滤波器 / 半带滤波器 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [host/include/uhd/usrp/dboard_manager.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/dboard_manager.hpp) | 子板管理器的公共接口：注册函数、`make`、前端名查询 |
| [host/lib/usrp/dboard_manager.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp) | 管理器实现：注册表、`dboard_key_t`、`init()` 造子板流程 |
| [host/include/uhd/usrp/dboard_base.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/dboard_base.hpp) | 子板基类层级：`rx_dboard_base` / `tx_dboard_base` / `xcvr_dboard_base` |
| [host/include/uhd/usrp/dboard_iface.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/dboard_iface.hpp) | 子板访问主板的接口（GPIO、SPI、时钟、`set_fe_connection`） |
| [host/include/uhd/usrp/fe_connection.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/fe_connection.hpp) | 前端连接类的公共接口 |
| [host/lib/usrp/fe_connection.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/fe_connection.cpp) | 前端连接实现：把字符串解析成采样模式与极性 |
| [host/lib/usrp/cores/rx_dsp_core_3000.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp) | 接收 DSP 核：DDC、抽取、增益补偿、消费 `fe_connection` |
| [host/lib/usrp/cores/rx_frontend_core_3000.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_frontend_core_3000.cpp) | 接收前端核：DC 偏置/IQ 平衡、同样消费 `fe_connection` |
| [host/lib/usrp/dboard/db_basic_and_lf.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.cpp) | 最简单的子板实现（Basic/LF），贯穿实践的主角 |

## 4. 核心概念与源码讲解

### 4.1 dboard_manager：子板的注册与生命周期

#### 4.1.1 概念说明

USRP 历史上出现过几十种子板（BasicRX、LFRX、WBX、SBX、UBX、TwinRX、Rhodium、ZBX……）。每一种子板都有不同的频率范围、增益范围、天线数量和接线方式。如果主板驱动里写一个巨大的 `switch(dboard_id)`，代码会极其臃肿且难以扩展。

UHD 的解法和设备工厂（u2-l1）、RFNoC 块注册表（u3-l2）一脉相承：**每种子板自己写一个实现文件，在程序启动时把自己登记进一个全局表；运行时主板读出子板 EEPROM 里的 `dboard_id`，到表里查到对应的构造函数，把子板造出来。** 负责这张表 + 造子板流程的对象，就是 `dboard_manager`。

可以把它理解成主板插槽上的一个「子板管家」：

- 每个主板插槽对应一个 `dboard_manager` 实例；
- 它知道这个插槽上插了什么子板（靠 EEPROM 的 id）；
- 它负责把子板的配置（频率、增益、天线、连接方式……）以 `rx_frontends/<name>` / `tx_frontends/<name>` 的形式挂到属性树上，让 `multi_usrp` 能用统一 API 访问。

#### 4.1.2 核心流程

子板从「插上主板」到「能用」要经过下面这条链：

```text
1. 静态注册期（main 之前）
   每个 db_*.cpp 用 UHD_STATIC_BLOCK 调用 dboard_manager::register_dboard
   → 把 (dboard_id → 构造函数, 子板名, 子设备名列表) 存进全局单例表
   get_id_to_args_map

2. 主板初始化期
   主板 impl（如 usrp2_impl / x300_radio_control）从子板 EEPROM 读出 id
   → 调用 dboard_manager::make(rx_id, tx_id, gdb_id, iface, subtree)

3. manager 构造期（dboard_manager_impl::init）
   a. 拿 id 去 get_id_to_args_map 里查 dboard_key_t（区分 单id / 收发对(xcvr)）
   b. set_nice_dboard_if()：先把 GPIO/时钟复位到安全默认值
   c. 若非 restricted，把 dboard_iface 挂到属性树 iface 节点
   d. 取出该 id 的「子设备名列表」，逐个调用 subdev_ctor(args) 造子板对象
   e. 每个造好的子板对象自己往 rx_frontends/<name> 子树里 create 各种属性
   f. 返回 _rx_frontends / _tx_frontends 名字列表（保持注册顺序）

4. 失败兜底
   若 init 抛异常 → 清掉半成品 → 用 "unknown" 子板(id=none)重新 init，保证不崩
```

子板按收发能力分三类，对应三种基类（见 `dboard_base.hpp`）：

| 基类 | 用途 |
| --- | --- |
| `rx_dboard_base` | 只能接收（如 BasicRX、LFRX） |
| `tx_dboard_base` | 只能发送（如 BasicTX、LFTX） |
| `xcvr_dboard_base` | 收发同体（如 WBX、SBX、UBX） |

注册时也分两种情况：单 id（纯收或纯发）与「收发对」(rx_id, tx_id)（xcvr），由 `dboard_key_t` 的 `_xcvr` 标志区分。

#### 4.1.3 源码精读

**① 注册函数的两种重载**。公共头声明了「单 id」和「收发对」两套 `register_dboard`：

`register_dboard`（单 id，纯收或纯发子板）—— [host/include/uhd/usrp/dboard_manager.hpp:L44-L48](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/dboard_manager.hpp#L44-L48)：

```cpp
static void register_dboard(const dboard_id_t& dboard_id,
    dboard_ctor_t db_subdev_ctor,            // 子设备构造函数指针
    const std::string& name,                 // 子板规范名，如 "Basic RX"
    const std::vector<std::string>& subdev_names = std::vector<std::string>(1, "0"),
    dboard_ctor_t db_container_ctor = NULL); // 可选：每子板一个"容器"对象
```

关键参数是 `subdev_names`：一块子板可以有多个子设备（通道），如双通道子板传 `{"0","1"}`。manager 会为每个名字各调用一次 `db_subdev_ctor`，造出一个独立前端。

`register_dboard`（收发对，xcvr 子板）—— [host/include/uhd/usrp/dboard_manager.hpp:L61-L66](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/dboard_manager.hpp#L61-L66) 用 `(rx_dboard_id, tx_dboard_id)` 两个 id 一起登记，表示收发同体。

**② dboard_key_t：查表用的复合键**。实现文件用一个内部类把「单 id」和「收发对」统一成一种键 —— [host/lib/usrp/dboard_manager.cpp:L24-L70](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L24-L70)：

```cpp
class dboard_key_t {
    dboard_id_t _rx_id, _tx_id;
    bool _xcvr;       // true=收发对, false=单 id
    bool _restricted; // restricted 子板的 iface 不挂属性树
    ...
};
```

`operator==`（[L72-L79](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L72-L79)）规定：两个键相等当且仅当「都是收发对且 rx/tx id 都相同」或「都是单 id 且 id 相同」。

**③ 全局注册表是一个单例字典** —— [host/lib/usrp/dboard_manager.cpp:L86-L94](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L86-L94)：

```cpp
typedef std::tuple<dboard_ctor_t, std::string,
                   std::vector<std::string>, dboard_ctor_t> args_t;
typedef uhd::dict<dboard_key_t, args_t> id_to_args_map_t;
UHD_SINGLETON_FCN(id_to_args_map_t, get_id_to_args_map)
```

`args_t` 是一个四元组：`(子设备构造函数, 规范名, 子设备名列表, 容器构造函数)`。`UHD_SINGLETON_FCN` 把它变成进程级单例，所有 `register_dboard` 都往这里写，所有 `make` 都从这里查。

`register_dboard_key`（[L96-L118](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L96-L118)）在登记前先检查 id 是否已注册，重复则抛 `key_error`——这能在链接期就发现「两个子板用了同一个 id」的配置错误。

**④ make 与构造期容错**。`dboard_manager::make` 有两个重载（按 id 或按 EEPROM），都最终 `new dboard_manager_impl(...)` —— [host/lib/usrp/dboard_manager.cpp:L233-L260](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L233-L260)。

构造函数把真正的初始化包进 `try/catch` —— [host/lib/usrp/dboard_manager.cpp:L265-L291](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L265-L291)：

```cpp
try {
    this->init(rx_eeprom, tx_eeprom, subtree, defer_db_init);
} catch (const std::exception& e) {
    UHD_LOGGER_ERROR("DBMGR") << "... Loading the \"unknown\" daughterboard ...";
    // 清掉半成品
    if (subtree->exists("rx_frontends")) subtree->remove("rx_frontends");
    ...
    dboard_eeprom_t dummy_eeprom; dummy_eeprom.id = dboard_id_t::none();
    this->init(dummy_eeprom, dummy_eeprom, subtree, false); // 兜底用 unknown 子板
}
```

这是一个非常重要的「降级」设计：**子板初始化失败不能让整个设备打不开**。比如一块子板的 EEPROM 被写坏了、或驱动有 bug，manager 会回退到 `db_unknown`（id=none），设备照样能开，只是这个前端不可用。`multi_usrp` 在 u2-l3 提到的「先选 subdev 再配通道」之所以必要，部分原因就是 unknown 子板的前端集合与真实子板不同。

**⑤ init()：查表 → 造子板 → 挂树**。`init` 是核心 —— [host/lib/usrp/dboard_manager.cpp:L293-L491](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L293-L491)。它先用 EEPROM id 遍历注册表找匹配键（[L299-L314](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L299-L314)），匹配规则是：

- 若是 xcvr 键：rx_id 与 tx_id 都对上才算「xcvr 匹配」；
- 若是单 id 键：rx 的 EEPROM id 对上就归为 rx 键，tx 同理。

找到键后，对每个子设备名调用构造函数造子板，并挂到 `rx_frontends/<name>` 子树（xcvr 分支 [L341-L392](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L341-L392)；纯收发分支 [L395-L490](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L395-L490)）。最后：

```cpp
_rx_frontends = rx_subdevs; // 保持注册时的顺序，供 get_rx_frontends() 返回
```

注意注释特意强调「不能用 `_rx_dboards.keys()`」，因为 dict 不保证插入顺序；前端顺序对通道映射（u2-l3 的 `rx_chan_to_mcp`）至关重要。

**⑥ 子板如何拿到它的运行环境**。构造函数收到的 `ctor_args_t` 实际是 `dboard_ctor_args_t*`（[host/lib/usrp/dboard_ctor_args.hpp:L18-L31](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_ctor_args.hpp#L18-L31)），里面装着子板需要的全部上下文：子设备名、`dboard_iface`（访问主板资源）、收发 EEPROM、收发子树、容器对象。子板基类 `dboard_base`（[host/include/uhd/usrp/dboard_base.hpp:L23-L54](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/dboard_base.hpp#L23-L54)）把这些藏进 pimpl，只暴露 `get_rx_subtree()` / `get_iface()` / `get_rx_id()` 等保护方法给子类用。

**⑦ dboard_iface：子板操控主板的「遥控器」**。子板不是孤立运行的，它要通过主板去拨 GPIO、发 SPI、开关时钟、设置前端连接。这套能力抽象成 `dboard_iface` —— [host/include/uhd/usrp/dboard_iface.hpp:L54-L308](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/dboard_iface.hpp#L54-L308)。它本身是个纯虚抽象类，每种子板插槽（usrp1/usrp2/b100/x300）各写一个子类实现，把虚函数翻译成对应主板的寄存器操作或 RPC。几个关键方法：

- GPIO 类：`set_gpio_ddr`（方向）、`set_gpio_out`（输出值）、`set_pin_ctrl`（GPIO 还是 ATR 控制）、`read_gpio`；
- 通信类：`write_spi` / `read_write_spi`、继承自 `i2c_iface` 的 I2C；
- 时钟类：`set_clock_rate` / `set_clock_enabled` / `get_codec_rate`；
- 前端连接：`set_fe_connection`（[L269-L271](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/dboard_iface.hpp#L269-L271)）—— 这是连接本讲 4.2 与 4.3 的桥梁。

manager 在构造期会（对非 restricted 子板）把 `iface` 也挂到属性树 `iface` 节点上（[dboard_manager.cpp:L331-L334](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L331-L334)），方便高层或调试直接操控。

#### 4.1.4 代码实践

**实践目标**：从源码层面验证「注册表是进程级单例」与「主板按插槽各造一个 manager」。

**操作步骤**：

1. 统计有多少个子板实现文件调用了 `register_dboard`，体会「自注册、无中央清单」。
2. 找出主板 impl 在哪里调 `dboard_manager::make`，确认「每个插槽/每块主板各造一个 manager」。

**第 1 步**用 Grep 在 `host/lib/usrp/dboard/` 下统计调用点（示例命令，可在仓库根目录运行）：

```bash
grep -rn "register_dboard" host/lib/usrp/dboard/ | grep -c "UHD_STATIC_BLOCK\|register_dboard("
```

**第 2 步**查看主板如何造 manager。例如 USRP2/N210 在每块主板循环里调用 —— [host/lib/usrp/usrp2/usrp2_impl.cpp:L805-L808](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp2/usrp2_impl.cpp#L805-L808)；X300 在 radio control 里调用 —— [host/lib/usrp/x300/x300_radio_control.cpp:L1579-L1582](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/x300/x300_radio_control.cpp#L1579-L1582)；B100 在 [host/lib/usrp/b100/b100_impl.cpp:L508-L511](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/b100/b100_impl.cpp#L508-L511)；USRP1 在 [host/lib/usrp/usrp1/usrp1_impl.cpp:L412-L415](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/usrp1/usrp1_impl.cpp#L412-L415)。

**需要观察的现象**：每块主板 impl 都把 manager 存进一个按主板/插槽索引的容器（usrp2 是 `_mbc[mb].dboard_manager`，usrp1 是 `_dbc[db].dboard_manager`），印证「一个插槽一个 manager」。

**预期结果**：`register_dboard` 的调用数等于「链接进 libuhd 的子板种类数 × 每种收发组合」；主板侧 `make` 调用点数量 = 主板/插槽数。这些调用都不需要硬件即可阅读确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `init()` 里要用 `dummy_eeprom`（id=none）兜底，而不是直接让异常向上抛？

**参考答案**：因为子板初始化失败属于「局部故障」（某块子板坏了或 EEPROM 错乱），不应导致整个主板设备 `device::make` 失败、让用户连设备都打不开。兜底成 `db_unknown` 后，设备仍可打开，只是该前端能力受限，便于用户排查。

**练习 2**：`dboard_manager_impl::init` 里为何要先 `set_nice_dboard_if()` 再造子板？

**参考答案**：[dboard_manager.cpp:L328](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L328) 与 [L509-L522](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard_manager.cpp#L509-L522) 把 GPIO 设为全输入、输出全低、关时钟，给子板一个干净、安全的初始电平，避免上一块子板残留的 GPIO/时钟状态影响新子板初始化（也在析构时复位，见 `~dboard_manager_impl`）。

---

### 4.2 fe_connection：射频前端的接线描述

#### 4.2.1 概念说明

子板把模拟射频信号变成数字 I/Q 后，要喂给 FPGA 的 DSP 核。但「I/Q 两根线怎么接」并不是统一的：

- 有的子板 I、Q 都接，且 I 在前（`IQ`，标准正交采样）；
- 有的把 I、Q 接反了（`QI`，需要软件交换）；
- 有的只有一路实信号（`I` 或 `Q`，实采样）；
- 有的子板是**外差架构**：模拟前端把信号搬到某个中频（IF），只用一路 ADC 采，再由 FPGA 数字下变频（`II`/`QQ` 表示两路都接同一根线）。

此外，硬件走线还可能把 I 或 Q 取反（交流耦合或变压器造成的极性翻转）。

如果让每个 DSP 核都自己判断这些差异，代码会到处重复。UHD 用一个不可变的小值对象 `fe_connection_t` 把「接线方式」抽象出来：它回答 5 个问题——采样模式、I/Q 是否交换、I 是否取反、Q 是否取反、IF 频率是多少。DSP 核只需读这个对象就能正确配置自己的多路复用寄存器。

#### 4.2.2 核心流程

`fe_connection_t` 有两个构造入口：

```text
入口 A：用字符串（最常用，写在子板属性 "connection" 里）
   "I"  → REAL,      无翻转
   "Ib" → REAL,      I 取反
   "IQ" → QUADRATURE,标准正交
   "QI" → QUADRATURE,IQ 交换
   "II" → HETERODYNE,外差（两路接同一根线）
   ...（b 表示该路取反）

入口 B：用 5 个独立字段
   (sampling_mode, iq_swapped, i_inverted, q_inverted, if_freq)
```

字符串用正则解析，规则见 `fe_connection.hpp` 的文档注释。关键判定：

- 只匹配一个字母（`I`/`Q`）→ **REAL**（实采样）；
- 匹配两个字母：
  - 两个字母相同（`II`/`QQ`）→ **HETERODYNE**（外差）；
  - 两个字母不同（`IQ`/`QI`）→ **QUADRATURE**（正交）；
- 字母后的 `b` 表示该路取反。

三种采样模式的物理含义：

| 模式 | 输入 | 输出 | 典型子板 |
| --- | --- | --- | --- |
| `QUADRATURE` | 复（I+Q） | 复 | WBX/SBX/UBX（零中频） |
| `REAL` | 实（单路） | 实 | BasicRX/LFRX（直接采样基带） |
| `HETERODYNE` | 实（单路） | 复 | 外差架构子板（需 IF→基带的数字下变频） |

外差模式有个关键参数 `if_freq`：FPGA 需要知道中频频率，才能用 NCO 把信号从中频搬到基带（见 4.3 的 `_dsp_freq_offset`）。

#### 4.2.3 源码精读

**① 三种采样模式枚举** —— [host/include/uhd/usrp/fe_connection.hpp:L22-L28](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/fe_connection.hpp#L22-L28)：

```cpp
enum sampling_t {
    QUADRATURE, // 复输入复输出
    HETERODYNE, // 实输入复输出（只用 I 或 Q 一路）
    REAL        // 实输入实输出
};
```

**② 字符串构造的正则解析** —— [host/lib/usrp/fe_connection.cpp:L28-L56](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/fe_connection.cpp#L28-L56)：

```cpp
static const std::regex conn_regex("([IQ])(b?)(([IQ])(b?))?");
std::cmatch matches;
if (std::regex_match(conn_str.c_str(), matches, conn_regex)) {
    if (matches[3].length() == 0) {
        // 只有一个字母: I / Q / Ib / Qb  → REAL
        _sampling_mode = REAL;
        _iq_swapped    = (matches[1].str() == "Q");
        _i_inverted    = (matches[2].length() != 0); // 是否带 b
        ...
    } else {
        // 两个字母
        _sampling_mode = (matches[1].str() == matches[4].str()) ? HETERODYNE
                                                                  : QUADRATURE;
        _iq_swapped    = (matches[1].str() == "Q");
        ...
    }
}
```

正则 `([IQ])(b?)(([IQ])(b?))?` 的设计很巧：第一组是首字母，第二组是可选的 `b`，第三组（含四、五组）是可选的「第二个字母+可选 b」。靠「第三组是否存在」区分实采样与复采样，靠「首字母与第四字母是否相同」区分外差与正交。

**③ 外差的取反一致性约束**。外差模式只接一根线，所以 I、Q 两个「取反位」要么都翻要么都不翻——[host/lib/usrp/fe_connection.cpp:L48-L50](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/fe_connection.cpp#L48-L50)：

```cpp
if (_sampling_mode == HETERODYNE and _i_inverted != _q_inverted) {
    throw uhd::value_error("Invalid connection string: " + conn_str);
}
```

非法字符串（如 `IQb`、空串）会落到 [L53-L55](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/fe_connection.cpp#L53-L55) 抛 `value_error`。

**④ 相等比较要容忍浮点误差** —— [host/lib/usrp/fe_connection.cpp:L58-L65](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/fe_connection.cpp#L58-L65)：四个离散字段直接比，但 `if_freq` 用 `uhd::math::frequencies_are_equal` 做容差比较，避免浮点精度导致「同一个连接被判成不等」。

#### 4.2.4 代码实践

**实践目标**：理解「天线选择 → 连接字符串 → fe_connection → DSP 核行为」这条链如何在子板代码里闭环。以 `db_basic_and_lf` 为例。

**操作步骤**：

1. 打开 [host/lib/usrp/dboard/db_basic_and_lf.hpp:L29-L33](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.hpp#L29-L33)，阅读天线模式到连接字符串的映射表：

   ```cpp
   static const uhd::dict<std::string, std::string> antenna_mode_to_conn{
       {"AB", "IQ"}, {"BA", "QI"}, {"A", "I"}, {"B", "Q"}};
   ```

   BasicRX 有 A、B 两个 SMA 口：选 `AB` 表示两路都接、`IQ` 正常顺序；选 `BA` 则两路接反、对应 `QI`（需要交换）；选 `A` 或 `B` 则只用一路、`I`/`Q`（实采样）。

2. 看 `basic_rx` 构造函数如何把这个映射写进属性树 —— [host/lib/usrp/dboard/db_basic_and_lf.cpp:L131-L140](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.cpp#L131-L140)：`antenna/value` 默认值与 `connection` 属性都由 `antenna_mode_to_conn[ant_mode]` 决定。

3. RFNoC 设备还注册了一个 **coerced subscriber**（[L150-L169](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.cpp#L150-L169)）：当用户改 `antenna/value` 时，自动联动更新 `connection` 与 `bandwidth`。

**需要观察的现象**：切换天线 `AB→BA` 时，`connection` 属性会从 `IQ` 变成 `QI`；这个 `connection` 字符串后续会被主板侧 radio control 读出，构造 `fe_connection_t`，喂给 DSP 核的 `set_mux`/`set_fe_connection`（见 4.3）。

**预期结果**：你能在脑中画出 `antenna/value (用户设置) → connection (字符串) → fe_connection_t (对象) → DSP 核寄存器` 的完整传播链。本实践为源码阅读型，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：连接字符串 `QbI` 表示什么？

**参考答案**：两个字母 `Q`、`I` 不同 → QUADRATURE（正交采样）；首字母是 `Q` → IQ 交换（`_iq_swapped=true`）；`Q` 后带 `b` → Q 路取反（注意：解析时按交换后的位置定 i/q，`_q_inverted=true`）。物理含义是「Q 接到了 I 输入端且极性翻转，I 接到了 Q 输入端」。

**练习 2**：为什么 `II`（两个 I）被归为 HETERODYNE 而不是报错？

**参考答案**：外差子板只用一路 ADC，但这路信号会被同时送到 DSP 核的 I 和 Q 两个输入端（硬件上把同一路接到两根线），于是字符串表现为「两个相同字母」。DSP 核据此进入 REAL_MODE+DOWNCONVERT 模式，用 NCO 把 IF 信号搬到基带，把实输入变成复输出。

---

### 4.3 cores：DSP 核如何消费 fe_connection

#### 4.3.1 概念说明

`host/lib/usrp/cores/` 下的每个 `.cpp` 都对应 FPGA 里一段固定的数字信号处理逻辑（一个「核」）。主机侧的 C++ 类不做信号处理本身——它只是这些 FPGA 寄存器的「驾驶员」：通过 `wb_iface`（wishbone 总线接口）`poke32` 写寄存器、`peek32` 读寄存器。

本讲聚焦三个核（目录里还有 time/vita/gpio/spi/i2c 等）：

| 核 | 作用 |
| --- | --- |
| `rx_dsp_core_3000` | 接收 DDC：CORDIC 频移 + 半带 + CIC 抽取 + 增益补偿 |
| `rx_frontend_core_3000` | 接收前端：DC 偏置、IQ 平衡、I/Q 映射（消费 fe_connection） |
| `tx_dsp_core_3000` / `tx_frontend_core_200` | 发送侧镜像 |

`_3000` 后缀指「3000 系列」架构（USRP N2x0/N3x0/X3x0 等），`_200` 指更早的 B100 系。它们都是同一思想的代际实现。

关键点：**`fe_connection` 最终就是被这些 DSP 核消费的**。子板把连接字符串写进属性树 `connection` 节点，主板 radio control 监听该节点，一旦变化就构造 `fe_connection_t` 并调用 `_rx_fe_map[chan].core->set_fe_connection(...)` 或 `->set_mux(...)`（见 [host/lib/usrp/x300/x300_radio_control.cpp:L307](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/x300/x300_radio_control.cpp#L307) 与 [L274](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/x300/x300_radio_control.cpp#L274)），DSP 核再把这几个布尔位翻译成多路复用寄存器。

#### 4.3.2 核心流程

**接收 DSP 核 `rx_dsp_core_3000` 的职责链**：

```text
用户设采样率 set_rx_rate
  → rx_dsp_core_3000::set_host_rate(rate)
     → 算出抽取因子 decim = tick_rate / rate
     → 按奇偶启用若干级半带滤波器（hb0/hb1/hb2）
     → 把 decim 和半带使能位写进 REG_DSP_RX_DECIM
     → 算 CIC 的算法增益，算出缩放系数，写 REG_DSP_RX_SCALE_IQ
     → 返回实际能给的采样率（tick_rate / decim_rate），可能与请求不同

子板/主板设前端连接 set_fe_connection / set_mux
  → 读 fe_conn 的采样模式与极性位
  → 拼出 REG_DSP_RX_MUX（REAL_MODE / SWAP_IQ / INVERT_I / INVERT_Q）
  → 若 HETERODYNE：把 if_freq 折算成 NCO 偏移 _dsp_freq_offset

用户设频率 set_rx_freq
  → set_freq(requested + _dsp_freq_offset)：把 NCO 调到 (请求频率 + IF偏移)
```

**抽取与半带的数学**。设 ADC 采样率（tick rate）为 \(f_\text{tick}\)，主机请求采样率为 \(f_s\)，则抽取因子：

\[
D = \text{round}(f_\text{tick} / f_s)
\]

`rx_dsp_core_3000` 的抽取链是「若干级半带（每级 ÷2）+ 一级 CIC（÷剩余奇数因子）」。代码里 [host/lib/usrp/cores/rx_dsp_core_3000.cpp:L142-L159](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L142-L159) 反复 `% 2` 来决定启用几级半带：能被 2 整除就启用一级、除以 2 继续；最多三级（B200 只支持两级）。

CIC（级联积分梳状）滤波器的算法增益是抽取因子 R、差分延迟 M、级数 N 的函数：

\[
G_\text{CIC} = (R \cdot M)^N
\]

UHD 的 CIC 参数为 \(M=1, N=4\)，故 [L199](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L199) 写 `std::pow(double(decim & 0xff), 4)`。这个增益会让信号幅度随抽取暴涨，必须在 DDC 出口用缩放系数抵消，否则会溢出。缩放系数（[L214](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L214)）：

```cpp
_scaling_adjustment = std::pow(2, ceil_log2(rate_pow)) / (1.648 * rate_pow);
```

其中 `1.648` 是 CORDIC 的渐近增益，`std::pow(2, ceil_log2(rate_pow))` 是 DDC 内已硬编码的 \(1/2^n\) 增益补偿。剩下因「用整数定点表示缩放系数」引入的微小误差，记在 `_fxpt_scalar_correction` 里，留给主机侧 `convert`（u4-l1）做最终修正。

#### 4.3.3 源码精读

**① 寄存器与 mux 标志位定义** —— [host/lib/usrp/cores/rx_dsp_core_3000.cpp:L18-L28](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L18-L28)：

```cpp
#define REG_DSP_RX_FREQ     _dsp_base + 0     // NCO 频率字
#define REG_DSP_RX_SCALE_IQ _dsp_base + 4     // 增益缩放系数
#define REG_DSP_RX_DECIM    _dsp_base + 8     // 抽取 + 半带使能
#define REG_DSP_RX_MUX      _dsp_base + 12    // IQ 映射（消费 fe_connection）
...
#define FLAG_DSP_RX_MUX_SWAP_IQ   (1 << 0)
#define FLAG_DSP_RX_MUX_REAL_MODE (1 << 1)
#define FLAG_DSP_RX_MUX_INVERT_Q  (1 << 2)
#define FLAG_DSP_RX_MUX_INVERT_I  (1 << 3)
```

**② set_mux：fe_connection → 寄存器** —— [host/lib/usrp/cores/rx_dsp_core_3000.cpp:L60-L101](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L60-L101)：

```cpp
void set_mux(const uhd::usrp::fe_connection_t& fe_conn) override {
    uint32_t reg_val = 0;
    switch (fe_conn.get_sampling_mode()) {
        case REAL:
        case HETERODYNE: reg_val = FLAG_DSP_RX_MUX_REAL_MODE; break;
        default:         reg_val = 0; break;  // QUADRATURE 不置位
    }
    if (fe_conn.is_iq_swapped())  reg_val |= FLAG_DSP_RX_MUX_SWAP_IQ;
    if (fe_conn.is_i_inverted())  reg_val |= FLAG_DSP_RX_MUX_INVERT_I;
    if (fe_conn.is_q_inverted())  reg_val |= FLAG_DSP_RX_MUX_INVERT_Q;
    _iface->poke32(REG_DSP_RX_MUX, reg_val);
    // HETERODYNE 时再算 _dsp_freq_offset（见下）
}
```

注意 REAL 与 HETERODYNE 都置 `REAL_MODE`（因为外差的输入端也只有一路实信号），区别在 HETERODYNE 还要额外做数字下变频。

**③ 外差 IF 折算成 NCO 偏移** —— [L82-L100](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L82-L100)：

```cpp
if (fe_conn.get_sampling_mode() == HETERODYNE) {
    const double fe_if_freq = fe_conn.get_if_freq();
    double if_freq = std::abs(std::fmod(fe_if_freq, _tick_rate)); // 折到 [0, tick)
    if (if_freq > (_tick_rate / 2.0)) if_freq -= _tick_rate;       // 折到 [-tick/2, tick/2]
    if (!std::signbit(fe_if_freq)) if_freq *= -1.0;                // 反向旋转抵消
    _dsp_freq_offset = if_freq;
} else {
    _dsp_freq_offset = 0.0;
}
```

这段在做「频率折叠」：物理 IF 可能远高于采样率，先按采样率取模找到**混叠后的等效频率**，再让 NCO 朝相反方向旋转，把信号从中频搬回基带。`_dsp_freq_offset` 之后会叠加进 `set_freq`（[L243-L252](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L243-L252)）：`set_freq(requested + _dsp_freq_offset)`。

**④ 增益缩放系数的写入与残差记录** —— `update_scalar`（[L225-L236](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L225-L236)）把缩放系数量化成整数写寄存器，同时把量化误差存进 `_fxpt_scalar_correction`，由 `get_scaling_adjustment()`（[L238-L241](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L238-L241)）交给主机侧 convert 做最终补偿——这正是 u4-l1 讲的 `set_scalar` 缩放系数的来源之一。

**⑤ otw 格式决定额外缩放**。`setup(stream_args)`（[L268-L293](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L268-L293)）按线上格式（sc16/sc8/sc12/fc32）设置 `_dsp_extra_scaling` 与 `_host_extra_scaling`，并支持 `peak`、`fullscale` 参数——把 u2-l5 讲的 otw_format 落到 DSP 核的具体缩放上。

**⑥ 前端核 rx_frontend_core_3000 同样消费 fe_connection** —— [host/lib/usrp/cores/rx_frontend_core_3000.cpp:L95-L145](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_frontend_core_3000.cpp#L95-L145)，逻辑与 `set_mux` 几乎镜像（拼 `REG_RX_FE_MAPPING` 寄存器），但额外管理 DC 偏置（`set_dc_offset`）、IQ 平衡（`set_iq_balance`）和外差 CORDIC 相位寄存器。这说明 `fe_connection` 会被**多个核各取所需**地消费。

**⑦ 核把自己的方法绑成属性树节点**。`rx_dsp_core_3000` 的 `populate_subtree`（[L295-L310](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L295-L310)）把 `rate/value`、`freq/value` 等绑上 coercer/publisher——这就是 u2-l4 讲的「desired/coerced + publisher」模型的落地：用户 `set_rx_rate` → 属性树 `rate/value` 的 coercer → `set_host_rate` → 返回 coerced 值回写。

#### 4.3.4 代码实践

**实践目标**：跟踪一条「设采样率」的完整链路，看清 DDC 抽取与半带启用之间的关系。

**操作步骤**：

1. 假设 tick rate \(f_\text{tick} = 100\,\text{MHz}\)，用户请求 \(f_s = 10\,\text{MHz}\)。手算抽取因子：\(D = 10\)。
2. 对照 `set_host_rate`（[L135-L219](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L135-L219)）逐步推演：
   - `decim_rate = 10`；
   - 第一次 `decim % 2 == 0` → `hb0=1`，`decim = 5`；
   - 第二次 `5 % 2 != 0` → 停；非 B200 下 `hb2` 也为 0；
   - 非半带的剩余抽取 `decim = 5` 写进 CIC；
   - 非 B200 分支：`hb_enable = 1`（只有 hb0），写 `REG_DSP_RX_DECIM = (1<<8) | 5`。
3. 验证 CIC 增益：`rate_pow = 5^4 = 625`；`_scaling_adjustment = 2^ceil(log2(625)) / (1.648 * 625) = 1024 / 1030 ≈ 0.994`。
4. 若改请求 \(f_s = 25\,\text{MHz}\)（\(D=4\)），推演应启用 hb0+hb1（`hb_enable=2`），剩余 CIC decim=1。

**需要观察的现象**：抽取因子可被 2、4、8 整除时，分别启用 1、2、3 级半带，CIC 只承担剩余奇数因子；奇数抽取（如 \(D=5\)）会触发警告 [L162-L169](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L162-L169) / [L183-L194](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/cores/rx_dsp_core_3000.cpp#L183-L194)，提示「无半带、通带会有 CIC 滚降」。

**预期结果**：你能从采样率请求推导出 `REG_DSP_RX_DECIM` 的每一位，并解释为什么 UHD 文档建议「尽量用偶数抽取」。本实践为源码推演型，无需硬件；如需实测，可在真实设备上 `uhd_usrp_probe` 后用 `rx_samples_to_file` 设不同采样率并观察日志中的 decimation 警告（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`set_mux` 里 REAL 和 HETERODYNE 为什么都置 `REAL_MODE` 位？

**参考答案**：因为这两种模式的**物理输入**都只有一路实信号（HETERODYNE 虽然输出复信号，但 ADC 只采了一路）。`REAL_MODE` 告诉 FPGA 的多路复用器「只取一路输入」，区别在于 HETERODYNE 还要额外启用数字下变频（由 `_dsp_freq_offset` 与前端核的 `DOWNCONVERT` 位共同完成）。

**练习 2**：为什么 `set_host_rate` 返回的是 `_tick_rate / decim_rate` 而不是用户请求的 `rate`？

**参考答案**：DDC 只能做整数抽取，且半带级数有限，硬件无法精确给出任意识别率的采样率。`clip(rate, true)` 先把请求 coerce 到支持的最接近档位，实际抽取因子 `decim_rate = round(tick_rate / coerced_rate)` 决定了真实输出率。回读这个值（即 u1-l6 强调的「必须回读实际采样率」）才能让后续 FFT、星座图等处理用对采样率。

**练习 3**：`_fxpt_scalar_correction` 这个残差最后去哪了？

**参考答案**：它通过 `get_scaling_adjustment()` 暴露给上层，在开流时作为 convert 子系统（u4-l1）的 `set_scalar` 缩放系数之一（再除以 32767），在主机侧把 sc16 定点样本还原成浮点时做最终增益修正，使整条链路的整体增益精确等于 1。

---

## 5. 综合实践

**任务**：以最简单的子板 `db_basic_and_lf` 为对象，把本讲三个最小模块串起来，画出「子板从注册到被 DSP 核驱动」的全链路。

**步骤**：

1. **注册阶段**：阅读 [host/lib/usrp/dboard/db_basic_and_lf.cpp:L69-L85](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.cpp#L69-L85) 的 `UHD_STATIC_BLOCK`。记录它注册了哪 8 个 (PID, 名字, 子设备名) 组合，并对照 [db_basic_and_lf.hpp:L10-L27](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.hpp#L10-L27) 解释为何 Basic/LF 各有「普通 PID」和「RFNOC PID」两套（提示：`RFNOC_PID_FLAG = 0x6300`，用于在新老设备上区分同名子板的不同工作模式）。

2. **造子板与挂树阶段**：阅读 `basic_rx` 构造函数 [db_basic_and_lf.cpp:L90-L178](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.cpp#L90-L178)。列出它在 `get_rx_subtree()` 下 `create` 了哪些属性节点。注意几个关键点：
   - 它**没有真正的增益**（Basic/LF 是无源子板），所以只 `create<int>("gains")` 一个「占位」属性让目录存在（注释明确写了 `phony property`）；频率范围才是它真正注册的能力——`freq/range` 设为 `(-_max_freq, +_max_freq)`（[L128-L130](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.cpp#L128-L130)），其中 `_max_freq` 对 Basic 是 250 MHz、对 LF 是 32 MHz。
   - 它通过 `get_iface()->set_clock_enabled(...)`、`set_pin_ctrl(...)`、`set_gpio_ddr(...)`（[L172-L177](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.cpp#L172-L177)）调用 `dboard_iface`——这正是 4.1 讲的「子板通过 iface 操控主板」。

3. **前端连接阶段**：在 `basic_rx` 构造里找到 `connection` 属性的初值来源（`antenna_mode_to_conn[ant_mode]`），并阅读 RFNoC 分支的 coerced subscriber（[L150-L169](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/dboard/db_basic_and_lf.cpp#L150-L169)），确认「改天线 → 改 connection」的联动。

4. **DSP 核阶段**：顺着 `connection` 字符串，说明它会被主板 radio control 读出、构造 `fe_connection_t`、最终调用 `rx_dsp_core_3000::set_mux` / `rx_frontend_core_3000::set_fe_connection`，把 `IQ`/`QI`/`I` 翻译成 `REG_DSP_RX_MUX` 的位。

**产出**：一张包含 4 个阶段、标注关键文件行号与核心 API 的流程图（手绘或文字版均可），并能回答：BasicRX 选 `BA` 天线时，`REG_DSP_RX_MUX` 的 `SWAP_IQ` 位会被置 1 吗？（答案：会，因为 `BA → QI → iq_swapped=true`。）

**说明**：Basic/LF 子板的频率范围注册与 `dboard_manager` 的对接是本综合实践的核心；增益部分因该子板无源而体现为「占位属性」，若你想看带真实增益注册的子板，可对照阅读 `host/lib/usrp/dboard/db_wbx_common.cpp` 或 `db_ubx.cpp`（不在本讲源码清单内，属扩展阅读）。

## 6. 本讲小结

- **dboard_manager** 是主板插槽上的子板管家：靠进程级单例注册表（`get_id_to_args_map`）把 `dboard_id` 映射到构造函数，运行时按 EEPROM id 查表造子板、把配置挂到 `rx_frontends`/`tx_frontends` 子树；构造失败会兜底成 `db_unknown` 而不让整机打不开。
- 子板分三类基类（`rx_dboard_base`/`tx_dboard_base`/`xcvr_dboard_base`），通过 `dboard_iface` 这只「遥控器」操控主板的 GPIO/SPI/I2C/时钟与前端连接；`dboard_ctor_args_t` 是子板拿到全部运行上下文的容器。
- **fe_connection** 用一个 4 字符字符串（`IQ`/`QI`/`I`/`II` 等）+ 可选 `b`（取反）精确描述射频前端到 FPGA 的接线，解析为 `(采样模式, IQ交换, I取反, Q取反, IF频率)` 五元组；三种采样模式 QUADRATURE/REAL/HETERODYNE 对应零中频、实采、外差三种前端架构。
- **cores** 下的 DSP 核（`rx_dsp_core_3000` 等）是 FPGA 寄存器的「驾驶员」，它们消费 `fe_connection` 配置多路复用寄存器、做 CORDIC 频移 + 半带 + CIC 抽取、补偿 CIC 算法增益 \((RM)^N\)，并把残差留给主机侧 convert（u4-l1）。
- 整条链路是：**子板 EEPROM id → dboard_manager 查表造子板 → 子板把 `connection` 字符串写进属性树 → 主板 radio control 构造 fe_connection_t → DSP 核 set_mux/set_fe_connection 写寄存器**——这就是一块子板被「认出、描述、驱动」的完整过程。
- 「读回值可能与请求不同」在这层也有体现：DDC 只能整数抽取，`set_host_rate` 返回的是 `_tick_rate/decim_rate`，频率范围、采样率范围都是 coerced 后的真实能力。

## 7. 下一步学习建议

- **向下到具体子板**：选一块带本振（LO）的真实收发子板精读，推荐 `host/lib/usrp/dboard/db_ubx.cpp` 或 RFNoC 时代的 `host/lib/usrp/dboard/rhodium/`、`zbx/`，看它们如何注册增益/频率范围、驱动 LO 合成器、并用 experts 框架（u3-l5）做属性传播。
- **向上到主板 radio control**：阅读 `host/lib/usrp/x300/x300_radio_control.cpp`，看它如何把 `dboard_manager`、DSP 核、`fe_connection`、property_tree 串成一个 radio 通道，并衔接 u4-l4 的 MPMD 设备实现。
- **横向对照 RFNoC**：现代设备的 DDC/DUC 已从 `cores/` 迁移到 RFNoC 块（u3-l6 的 `ddc_block_control`/`duc_block_control`），可对比 `rx_dsp_core_3000` 与 `ddc_block_control` 的异同，理解「老核驱动 vs RFNoC 块控制器」两代架构的演进。
- **校准衔接**：本讲出现的 `_scaling_adjustment`、IQ 平衡、DC 偏置等正是 u5-l4 校准子系统的修正对象，学完校准后可回看 `rx_frontend_core_3000::set_iq_balance`/`set_dc_offset`，理解校准数据如何最终落到这些寄存器。
