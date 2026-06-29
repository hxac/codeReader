# 校准子系统与校准工具

## 1. 本讲目标

本讲深入 UHD 的「校准数据子系统」（calibration subsystem）。学完后你应该能够：

- 说清校准数据在 UHD 生命周期中的角色：它是什么、为什么需要、什么时候被加载。
- 理解 `container` 抽象基类与 FlatBuffers 序列化机制如何把「内存中的校准表」与「磁盘上的 `.cal` 文件」相互转换。
- 区分四类校准数据（IQ / IQ-DC / DSA / 功率）各自修正的射频失真，并指出对应的 `.fbs` 模式文件。
- 掌握 `uhd::usrp::cal::database` 的「三源优先级」存储与查找模型（RC / FILESYSTEM / FLASH）。
- 读懂 `uhd_cal_tx_iq_balance` 等校准工具如何「自校准」并把结果写回数据库。

## 2. 前置知识

本讲是专家层内容，假设你已经掌握：

- **UHD 设备模型**：`multi_usrp`、子板（daughterboard）、属性树（见 u2-l3、u2-l4）。
- **收发流式 API**：`rx_streamer::recv`、`tx_streamer::send`、`rx_metadata_t`（见 u2-l5～u2-l7）。
- **子板与前端抽象**：增益、频率、天线、前端连接（见 u4-l5）。

补几个本讲要用到的概念：

- **校准（calibration）**：真实射频硬件的增益、相位、绝对功率都和理想值有偏差，且随频率、温度变化。校准就是「测出这些偏差并存成一张表」，运行时查表补偿，使硬件表现得接近理想。
- **失真（impairment）**：偏离理想的误差。常见的有 IQ 失衡（I/Q 两路增益不等、相位不正交）、直流偏移（LO 泄漏 / DC offset）、增益不准、绝对功率不准。
- **FlatBuffers**：Google 的一个零拷贝、前向/后向兼容的序列化库。UHD 用它把校准表编码成紧凑的二进制 `.cal` 文件。它的「模式（schema）」用 `.fbs` 文件描述。
- **资源编译器（Resource Compiler, cmrc）**：把小文件在编译期嵌入共享库，运行时像读文件系统一样读取，无需外部文件依赖。

## 3. 本讲源码地图

本讲涉及的关键文件分四组：

| 文件 | 作用 |
|------|------|
| `host/include/uhd/cal/container.hpp` | 所有校准表的抽象基类，定义 `serialize()/deserialize()` 与模板工厂 `make<T>()`。 |
| `host/include/uhd/cal/iq_cal.hpp` / `host/lib/cal/iq_cal.cpp` | IQ 失衡校准表的接口与实现（含 FlatBuffers 序列化、按频率插值）。 |
| `host/include/uhd/cal/iq_dc_cal.hpp` | 宽带多抽头 IQ + DC 校准表接口。 |
| `host/include/uhd/cal/dsa_cal.hpp` | ZBX 子板的 DSA（数字步进衰减器）校准表接口。 |
| `host/include/uhd/cal/pwr_cal.hpp` | 功率校准表接口（gain↔power 映射）。 |
| `host/include/uhd/cal/*.fbs` | 五个 FlatBuffers 模式文件，定义各类校准数据的二进制布局。 |
| `host/include/uhd/cal/database.hpp` / `host/lib/cal/database.cpp` | 校准数据库：按 `key+serial` 读写，三源优先级查找。 |
| `host/utils/usrp_cal_utils.hpp` | 校准工具共享的辅助函数（建设备、发波形、捕获、存结果）。 |
| `host/utils/uhd_cal_tx_iq_balance.cpp` | TX IQ 失衡自校准工具主程序。 |
| `host/lib/rc/CMakeLists.txt` | 把出厂 `.cal` 文件编译进 `libuhd` 的资源编译器配置。 |
| `host/lib/usrp/common/apply_corrections.cpp` | 设备驱动加载并应用 IQ 校准的典型消费者。 |
| `host/tests/cal_database_test.cpp` | database 的单元测试，演示读写与备份行为。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. 校准数据模型：`container` 基类与 FlatBuffers 序列化（含四类校准类型全景）。
2. `iq_cal`：按频率插值的 IQ 失衡校正容器。
3. `cal database`：三源优先级的校准数据库。
4. `uhd_cal_*` 校准工具与 `usrp_cal_utils`。

### 4.1 校准数据模型：container 基类与 FlatBuffers 序列化

#### 4.1.1 概念说明

校准数据的本质是「一张表」：在若干离散频率点（或频率×增益点）上测出补偿值，运行时按当前工作频率查表/插值取用。

不同种类的校准（IQ 失衡、功率、DSA……）表结构不同，但它们有共同的生命周期：

- **产生**：由校准工具测量得到，或在出厂时固化。
- **持久化**：序列化成二进制存到磁盘（或嵌入库、或烧进 Flash）。
- **加载**：设备初始化时反序列化回内存对象。
- **消费**：驱动在调谐（tune）或设增益时查表补偿。

UHD 用一个抽象基类 `container` 统一这套生命周期，把「怎么序列化/反序列化」交给子类实现，从而让数据库 `database` 可以「不关心具体类型」地搬运二进制大对象（BLOB）。

> 关键设计：`database` 只存取 `std::vector<uint8_t>`（裸字节），它**不解释**格式；解释格式是 `container` 子类的职责。两者通过 FlatBuffers 字节流解耦。

#### 4.1.2 核心流程

校准数据的一次完整「产生→消费」流程：

```
校准工具测量
   │  cal_data->set_cal_coeff(...)   （往内存表里填系数）
   ▼
序列化
   │  bytes = cal_data->serialize()  （FlatBuffers 编码）
   ▼
持久化
   │  database::write_cal_data(key, serial, bytes)
   ▼ （写盘 / 嵌入 RC / 烧 Flash）
……设备下次初始化……
   ▼
查找读取
   │  bytes = database::read_cal_data(key, serial)
   ▼
反序列化
   │  cal = container::make<iq_cal>(bytes)   （模板工厂 + deserialize）
   ▼
消费
   │  coeff = cal->get_cal_coeff(freq)        （驱动在 tune 时调用）
```

FlatBuffers 的好处在这里很突出：反序列化时 `GetIQCalCoeffs(ptr)` 只是返回一个指向缓冲区的视图对象，**不做拷贝、不做解析**，真正读取某字段时才按偏移量取值。这非常适合「一次性加载、频繁查询」的校准表。

#### 4.1.3 源码精读

`container` 基类非常薄，只规定契约：每个校准表都要能报出自己的名字、序列号、时间戳，并能自我序列化/反序列化。见 [host/include/uhd/cal/container.hpp:22-54](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/container.hpp#L22-L54)，其中 `serialize()` 与 `deserialize()` 是纯虚函数（第 37、40 行），由各子类实现。

真正巧妙的是它的**模板工厂** [host/include/uhd/cal/container.hpp:47-53](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/container.hpp#L47-L53)：传入一段序列化字节，它先用子类的默认 `make()` 造一个空对象，再调 `deserialize()` 填充，从而把「任意 `container` 子类」从字节流里还原出来。消费者只需写 `container::make<iq_cal>(bytes)` 即可。

所有四类校准表的二进制布局都由 FlatBuffers 模式描述。公共部分是 `Metadata` 表 [host/include/uhd/cal/cal_metadata.fbs:8-15](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/cal_metadata.fbs#L8-L15)，包含 `name`/`serial`/`timestamp` 以及校准数据自身的版本号 `version_major`/`version_minor`——后者用于反序列化时做版本兼容检查。

四类校准数据全景（本讲实践任务的核心）：

| 类型 | 头文件 | FlatBuffers 模式 | 修正的失真 | 数据形态 |
|------|--------|------------------|-----------|----------|
| **IQ 失衡** | `iq_cal.hpp` | [iq_cal.fbs](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/iq_cal.fbs) | I/Q 增益不等与相位不正交导致的**镜像频率**（image）；兼用于 Gen-3 DC 偏移 | 每个频率一个复数系数 `(real,imag)` + 抑制量 |
| **IQ-DC（宽带）** | `iq_dc_cal.hpp` | [iq_dc_cal.fbs](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/iq_dc_cal.fbs) | **宽带（多抽头）** IQ 失衡 + **DC 偏移**，含缩放因子与群时延 | 每个频率一组抽头向量 `icross[]`/`qinline[]` + DC + 延迟 |
| **DSA** | `dsa_cal.hpp` | [dsa_cal.fbs](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/dsa_cal.fbs) | ZBX 前端**数字步进衰减器**的增益不准：把「频段×增益档」映射到精确的衰减器档位 | 频段 → 61 个增益档 → 各档 DSA 设置数组 |
| **功率** | `pwr_cal.hpp` | [pwr_cal.fbs](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/pwr_cal.fbs) | **绝对功率不准**：建立 gain(dB)↔power(dBm) 映射，支持按功率反查增益 | 温度 → 频率 → {gain→power} 表 + min/max 功率 |

每个 `.fbs` 都带一个 4 字节 `file_identifier`（如 `iq_cal.fbs` 第 32 行的 `"IQ/f"`、`pwr_cal.fbs` 的 `"dB/m"`），FlatBuffers 可据此快速识别文件类型。

#### 4.1.4 代码实践

**实践目标**：建立「失真类型 → 校准类 → `.fbs` 模式」的对应关系。

**操作步骤**：

1. 打开 `host/include/uhd/cal/` 目录，浏览五个 `.fbs` 文件。
2. 对照上表，逐个确认每个 `.fbs` 顶部注释里写的「这是什么校准」。
3. 注意 `cal_metadata.fbs` 被 `include "cal_metadata.fbs";` 进其余四个文件，理解「公共元数据 + 专用数据」的分层。

**需要观察的现象**：四个专用 `.fbs` 各自定义了不同的 `root_type`（`IQCalCoeffs` / `IQDCCalCoeffs` / `DsaCal` / `PowerCal`），但都内嵌同一个 `Metadata` 表。

**预期结果**：你能不查表地说出「IQ 失衡用 `iq_cal.fbs`、功率用 `pwr_cal.fbs`」，并解释为什么功率表需要温度维度而 IQ 表不需要（绝对功率对温度敏感，IQ 失衡相对不敏感）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `container::make<T>()` 是模板函数，而 `database::read_cal_data()` 只返回 `vector<uint8_t>`？

**答案**：`database` 故意做「类型无关」的存储，只搬运字节，这样它不必认识每一种校准类型；具体「字节→对象」的类型转换由消费者用 `container::make<T>()` 显式指定 `T`（如 `iq_cal`、`pwr_cal`）来完成。这把「存储」与「类型解释」解耦。

**练习 2**：FlatBuffers 的 `Metadata` 里为什么要有 `version_major`/`version_minor`？

**答案**：校准数据格式会演进。反序列化时（如 `iq_cal.cpp` 第 162 行 `UHD_ASSERT_THROW(... == VERSION_MAJOR)`）会校验主版本号，避免用旧格式数据驱动新代码或反之，保证前向/后向兼容。

---

### 4.2 iq_cal：按频率插值的 IQ 失衡校正容器

#### 4.2.1 概念说明

`iq_cal` 是最经典、也是最常用的校准类型，修正的是 **IQ 失衡（IQ imbalance）**。

理想 I/Q 解调要求两路增益严格相等、相位严格正交。实际硬件做不到，结果是一个频率为 \(f\) 的信号会在镜像频率 \(-f\) 处也产生一份「残影」，称为**镜像（image）**。镜像会落入邻道、抬高底噪、破坏调制质量。

`iq_cal` 的做法：在不同频率点测出一个复数校正系数，运行时按当前频率查表并**插值**，再把系数喂给设备的 IQ 平衡寄存器（如 `set_tx_iq_balance(coeff)`）。类注释明确说明它服务 Gen-2/Gen-3 的 TX DC 偏移与 RX/TX IQ 失衡，见 [host/include/uhd/cal/iq_cal.hpp:19-24](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/iq_cal.hpp#L19-L24)。

#### 4.2.2 核心流程

`iq_cal` 在内部用 `std::map<double, std::complex<double>>` 按频率存系数（有序），查询时做两种插值：

- **最近邻（NEAREST_NEIGHBOR）**：取离查询频率最近的已知点。
- **线性（LINEAR）**：在相邻两已知点之间做线性插值。

线性插值公式（实部、虚部分别插值）：

\[
c(f) = c_{lo} + (c_{hi} - c_{lo}) \cdot \frac{f - f_{lo}}{f_{hi} - f_{lo}}, \quad f \in [f_{lo}, f_{hi}]
\]

边界外（查询频率小于最小点或大于最大点）直接取端点值，不外推。

序列化时，把 `map` 里每个 `(freq, coeff)` 打包成一个 FlatBuffers `IQCalCoeff` 结构体（`freq, coeff_real, coeff_imag, suppression_abs, suppression_delta`，见 [iq_cal.fbs:16-23](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/iq_cal.fbs#L16-L23)），整体放进 `IQCalCoeffs` 表，并用 `FinishIQCalCoeffsBuffer` 封装。

#### 4.2.3 源码精读

插值逻辑在 [host/lib/cal/iq_cal.cpp:61-93](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/iq_cal.cpp#L61-L93)：先用 `lower_bound(freq)` 定位到第一个 ≥ freq 的点，据此分出「比 freq 大/小」两侧，再按 `_interp` 模式选择最近邻或 `linear_interp` 对实部、虚部各插一次。端点越界分支在第 67-76 行处理。

`set_cal_coeff` 直接写 map，见 [host/lib/cal/iq_cal.cpp:95-102](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/iq_cal.cpp#L95-L102)；注意它额外存了一组「抑制量」`_supp`（绝对抑制 dB、相对改善 dB），这是校准工具用来评估系数质量的，运行时其实用不到。

序列化实现在 [host/lib/cal/iq_cal.cpp:113-150](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/iq_cal.cpp#L113-L150)：先用 `std::transform` 把 map 转成 `IQCalCoeff` 向量（第 126-137 行），再 `CreateMetadataDirect` 写元数据，`CreateIQCalCoeffsDirect` 建表，`FinishIQCalCoeffsBuffer` 收尾，最后把 builder 缓冲区拷成 `vector<uint8_t>` 返回。

反序列化 [host/lib/cal/iq_cal.cpp:154-174](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/iq_cal.cpp#L154-L174) 先用 `Verifier` 校验缓冲区合法性（防损坏/篡改），再 `GetIQCalCoeffs` 拿视图，校验主版本号，最后把系数回填进 map。

#### 4.2.4 代码实践

**实践目标**：用源码确认 `iq_cal` 的「测量→存表→查表」闭环。

**操作步骤**：

1. 在 `host/utils/usrp_cal_utils.hpp` 找到 `store_results()`（[第 160-178 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/usrp_cal_utils.hpp#L160-L178)），看它如何 `iq_cal::make(...)` 后逐点 `set_cal_coeff`，最后 `database::write_cal_data(cal_key, serial, cal_data->serialize())`。
2. 在 `host/lib/usrp/common/apply_corrections.cpp` 找到消费侧（[第 93-96 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/common/apply_corrections.cpp#L93-L96)）：`has_cal_data` → `read_cal_data` → `container::make<iq_cal>(cal_data)`，把字节还原成对象并缓存。
3. 注意 `cal_key = xx + "_" + what`（如 `tx_iq`）就是写和读两端约定的字符串 key，序列号是子板 serial——两端必须一致才能匹配。

**需要观察的现象**：写端用 `tx_iq` + 子板 serial 存，读端用同样的 `file_prefix`（即 `tx_iq`）+ 同一 serial 取。key/serial 是贯穿始终的「主键」。

**预期结果**：你能画出 `iq_cal` 从 `store_results` 写入、到 `apply_corrections` 读出、再到 `get_cal_coeff(freq)` 查询的完整链路。若无硬件，此为「源码阅读型实践」，**待本地验证**运行时行为。

#### 4.2.5 小练习与答案

**练习 1**：如果查询频率大于表里最大的已知频率点，`get_cal_coeff` 会怎样？

**答案**：见 [iq_cal.cpp:67-71](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/iq_cal.cpp#L67-L71)，`lower_bound` 返回 `end()`，函数直接返回最大频率点的系数，**不外推**。所以校准表的频率范围必须覆盖设备实际工作频段，否则边界处用不上好系数。

**练习 2**：`set_cal_coeff` 存的 `suppression_abs`/`suppression_delta` 在运行时有用吗？

**答案**：没有。它们是校准工具评估「这个系数把镜像压了多少 dB」的诊断信息，运行时驱动只取复数系数本身（见 `iq_cal.cpp:169-172` 注释「Suppression levels are really not necessary for runtime」），但仍一并序列化以便后续审查校准质量。

---

### 4.3 cal database：三源优先级的校准数据库

#### 4.3.1 概念说明

校准数据可以存在好几个地方，优先级不同：

- **RC（Resource Compiler）**：出厂时硬编码进 `libuhd` 的 `.cal` 文件，对**整族设备**通用，**与序列号无关**。优先级最低。
- **FLASH（EEPROM/Flash）**：烧在设备非易失存储里，**按序列号**区分。设备驱动注册回调来读。优先级中等。
- **FILESYSTEM**：主机本地磁盘上的 `.cal` 文件，由校准工具生成，**按序列号**区分。优先级最高（用户/工具的最新测量结果应覆盖出厂值）。

概念上还有 **USER**（用户在内存里直接提供的对象，最高）和 **NONE/ANY**。`source` 枚举按「优先级从低到高」排列，见 [host/include/uhd/cal/database.hpp:17-29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/cal/database.hpp#L17-L29)。

`database` 类是这套存储的统一入口：用 `(key, serial)` 二元组索引，`key` 描述校准类型（如 `"rx_iq"`、`"tx_iq"`、`"zbx_dsa_tx"`），`serial` 标识具体设备。

#### 4.3.2 核心流程

读取时的优先级查找（`source::ANY` 时按优先级试每个源）：

```
read_cal_data(key, serial, source_type):
  for (源 in [FILESYSTEM, FLASH, RC]):      # 按优先级，见 data_fns 数组
      if source_type 匹配 且 该源 has_cal_data(key, serial):
          return 该源的 get_cal_data(key, serial)
  throw key_error("Calibration Data not found...")
```

三个源各自的路径规则：

| 源 | 路径/定位方式 | 是否用 serial |
|----|--------------|--------------|
| FILESYSTEM | `get_cal_data_path() / key_serial.cal` | 是 |
| FLASH | 遍历设备驱动注册的回调 | 是 |
| RC | 库内虚拟路径 `cal/<key>.cal` | 否（族级通用） |

写入只支持 FILESYSTEM（`write_cal_data` 暗含 `source::FILESYSTEM`），且会**先备份**旧文件再覆盖。

#### 4.3.3 源码精读

优先级数组是核心，见 [host/lib/cal/database.cpp:199-203](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L199-L203)：`data_fns` 把 `(source, has_fn, get_fn)` 三元组按 FILESYSTEM→FLASH→RC 排列，注释明确「These are in order of priority!」。`read_cal_data` 与 `has_cal_data` 都遍历这个数组，见 [database.cpp:210-225](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L210-L225) 与 [227-239](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L227-L239)，用一个表达式同时支持「指定源」和「ANY 任取最高优先级」两种模式。

RC 实现（[database.cpp:40-67](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L40-L67)）通过 cmrc 打开虚拟文件 `cal/<key>.cal`，`has_cal_data_rc` 直接忽略 serial 参数——因为 RC 数据按定义是族级通用、不能绑定单个序列号（`database.hpp` 第 60-64 行的注释强调这一点）。

FILESYSTEM 实现（[database.cpp:116-154](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L116-L154)）把文件名拼成 `key_serial.cal`，读取前用 `CALDATA_MAX_SIZE`（10 MiB，[第 34 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L34)）做大小校验，防止恶意/误操作的大文件撑爆内存。

`write_cal_data` 的备份逻辑见 [database.cpp:241-265](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L241-L265)：若目标文件已存在，把它重命名为 `key_serial.cal.<ext>`（`ext` 默认是 POSIX 时间戳，也可自定义），再写新文件——旧校准永远不丢，只归档。

FLASH 由设备驱动通过 `register_lookup` 注册回调，见 [database.cpp:267-273](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L267-L273)，回调存进一个进程级单例 `lookup_registry`（[164-188](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L164-L188)）。注意 `UHD_ASSERT_THROW(source_type == source::FLASH)`——目前只允许注册 FLASH 源。

磁盘根目录由 `uhd::get_cal_data_path()` 决定，见 [host/lib/utils/paths.cpp:292-303](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/utils/paths.cpp#L292-L303)：优先环境变量 `UHD_CAL_DATA_PATH`，否则 `$XDG_DATA_HOME/uhd/cal`。

#### 4.3.4 代码实践

**实践目标**：通过单元测试观察 database 的读写与备份行为（无需硬件）。

**操作步骤**：

1. 阅读 [host/tests/cal_database_test.cpp:19-34](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/cal_database_test.cpp#L19-L34) 的 `test_rc`：它断言 RC 源里存在 `test` 这个 key（对应 `host/lib/rc/cal/test.cal`），且**忽略 serial**（传 `"1234"` 也能命中），但 FILESYSTEM 源里不存在。
2. 阅读 [cal_database_test.cpp:36-90](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/tests/cal_database_test.cpp#L36-L90) 的 `test_fs`：它临时改写 `UHD_CAL_DATA_PATH` 指向一个临时目录，写入 `mock_data` 字节，读回比对；然后对同一 `(key,serial)` 写两次（第二次带 `BACKUP` 后缀），断言生成了 `mock_data_abcd.cal.BACKUP` 备份文件。
3. 可选：若已构建 UHD，运行 `ctest -R cal_database_test` 观察通过情况（**待本地验证**）。

**需要观察的现象**：RC 查找与 serial 无关；FILESYSTEM 写入会自动给旧数据加时间戳/自定义后缀备份，不会直接覆盖丢失。

**预期结果**：理解「优先级」不仅是查找顺序，也意味着「FILESYSTEM 的新测量天然覆盖 RC 的出厂值」，而旧 FILESYSTEM 数据被安全归档。

#### 4.3.5 小练习与答案

**练习 1**：为什么 RC 源要忽略 serial？

**答案**：RC 数据在编译期嵌入 `libuhd`，对一整族设备通用（如所有 ZBX 子板的出厂 DSA 表）。它不可能为每个具体序列号单独存一份。序列号只在 FILESYSTEM/FLASH 这类「设备特有」数据中有意义（见 `database.hpp:60-64`）。

**练习 2**：`write_cal_data` 为什么不直接覆盖旧文件？

**答案**：见 [database.cpp:251-259](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L251-L259)：它先把旧文件重命名为带时间戳的备份，再写新文件。校准数据是用户花时间测量的资产，重测时归档旧数据可以回滚或对比，避免误操作造成不可恢复的丢失。

---

### 4.4 uhd_cal_* 校准工具与 usrp_cal_utils

#### 4.4.1 概念说明

`database` 负责存取校准，但校准数据**从哪来**？答案是校准工具（calibration utilities）。UHD 提供三个自校准工具，都遵循同一套「**自发自收、迭代寻优、写回数据库**」的范式：

| 工具 | 修正对象 | key |
|------|---------|-----|
| `uhd_cal_tx_iq_balance` | 发射 IQ 失衡 | `tx_iq` |
| `uhd_cal_rx_iq_balance` | 接收 IQ 失衡 | `rx_iq` |
| `uhd_cal_tx_dc_offset` | 发射 DC 偏移 | `tx_dc` |

它们都是命令行程序，依赖一个**支持自校准**的子板（必须有 `CAL` 收发天线、有合法序列号、属于受支持的型号），通过把发射口环回到接收口，测量镜像/泄漏并最小化它。

三个工具共享同一组辅助函数（建设备、发连续波、捕获样本、算功率、存结果），抽到 `usrp_cal_utils.hpp` 里。

#### 4.4.2 核心流程

以 `uhd_cal_tx_iq_balance` 为例（[host/utils/uhd_cal_tx_iq_balance.cpp:54-283](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_cal_tx_iq_balance.cpp#L54-L283)）：

```
1. setup_usrp_for_cal():   建设备（带 ignore_cal_file=1，禁用已有校准避免污染测量）
                           选子板、设 CAL 天线、按型号设默认采样率
2. 起一个 tx_thread:       连续发射一个单音（sine wave）
3. 对每个频率点 tx_lo (freq_start → freq_stop, step freq_step):
     a. tune_rx_and_tx():   把 RX LO 偏移到 TX LO 附近（用 rx_offset 避开 DC）
     b. set_optimal_rx_gain(): 自动找不削波的 RX 增益
     c. 在 (相位, 幅度) 二维网格上搜索 IQ 校正系数:
          - 对每个候选系数 set_tx_iq_balance(correction)
          - 捕获样本, 计算 tone 处功率与镜像处功率之差 = 抑制量
          - 记录使抑制量最大的系数
        网格逐步细化, 直到步长 < precision
     d. 若优于初始抑制, 存入 results
4. store_results("TX","tx","iq",serial):
     iq_cal::make() → 逐点 set_cal_coeff → database::write_cal_data("tx_iq", serial, serialize())
```

关键度量是「**抑制量 suppression**」：tone 频率处的 dB 功率 减去 镜像频率处的 dB 功率。抑制越大，镜像越小，系数越好。

#### 4.4.3 源码精读

主程序骨架 [host/utils/uhd_cal_tx_iq_balance.cpp:54-83](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_cal_tx_iq_balance.cpp#L54-L83)：`UHD_SAFE_MAIN` + `boost::program_options` 解析选项（频率起止、步长、波形、精度等），与 u1-l6 讲过的示例骨架一致。

搜索循环是核心，见 [uhd_cal_tx_iq_balance.cpp:208-253](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_cal_tx_iq_balance.cpp#L208-L253)：在 `(phase_corr, ampl_corr)` 二维区间内遍历候选系数（第 209-214 行），对每个候选 `set_tx_iq_balance` 后捕获并算抑制（第 216-237 行），保留最优；然后以最优点为中心缩小区间、缩小步长（第 246-252 行），直到步长小于 `precision`——这是一种**坐标下降式**的多级网格搜索。

把结果写回数据库在 `store_results()`，见 [host/utils/usrp_cal_utils.hpp:160-178](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/usrp_cal_utils.hpp#L160-L178)：构造 `iq_cal`，逐点 `set_cal_coeff(freq, {real,imag}, best, delta)`，然后 `write_cal_data(xx+"_"+what, serial, cal_data->serialize())`。对 TX IQ 而言 key 就是 `"tx_iq"`，正好和 `apply_corrections.cpp` 读取时用的 `file_prefix` 对上。

「发连续波」由 `tx_thread` 完成，见 [usrp_cal_utils.hpp:355-427](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/usrp_cal_utils.hpp#L355-L427)：它把 1 秒长、整周期对齐的正弦波填进缓冲，循环按帧发送，避免拼接处产生不连续。

`setup_usrp_for_cal` 里的关键细节 [usrp_cal_utils.hpp:228-261](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/usrp_cal_utils.hpp#L228-L261)：建设备时追加 `ignore_cal_file=1,ignore-cal-file=1`（第 231 行），**禁用已有校准**，否则旧校准会扭曲本次测量；它还强制要求 `CAL` 天线和合法 serial（`check_for_empty_serial`）。

#### 4.4.4 代码实践

**实践目标**：读懂「测量结果如何变成数据库里的 `.cal` 文件」。

**操作步骤**：

1. 在 `store_results()`（[usrp_cal_utils.hpp:160-178](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/usrp_cal_utils.hpp#L160-L178)）跟踪：`results`（一个 `result_t` 数组，含 `freq/real_corr/imag_corr/best/delta`）如何被填进 `iq_cal` 再 `serialize`。
2. 对照 [uhd_cal_tx_iq_balance.cpp:254-262](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_cal_tx_iq_balance.cpp#L254-L262) 看 `result_t` 各字段的来源：`real_corr=best_ampl_corr`、`imag_corr=best_phase_corr`、`best=best_suppression`、`delta=best-initial`。
3. 若有支持的硬件（如带 WBX/SBX/UBX 子板的 N2xx/X3xx），可运行 `uhd_cal_tx_iq_balance --args <addr> --verbose`，观察每个频率点打印的抑制量，完成后查看 `$XDG_DATA_HOME/uhd/cal/tx_iq_<serial>.cal` 是否生成（**待本地验证**，需硬件）。

**需要观察的现象**：搜索过程中 `verbose` 会逐点打印「TX IQ: X MHz: best suppression Y dB, corrected Z dB」；`delta` 为正说明本次校正确实改善了镜像。

**预期结果**：理解工具产出的 `tx_iq_<serial>.cal` 文件，正是 4.2 节 `apply_corrections` 运行时会读取、`iq_cal` 会反序列化的那份字节流——四个模块由此闭环。若无硬件，改为绘制「搜索→存表→读取→插值应用」的状态流转图。

#### 4.4.5 小练习与答案

**练习 1**：为什么校准工具建设备时要加 `ignore_cal_file=1`？

**答案**：见 [usrp_cal_utils.hpp:231](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/usrp_cal_utils.hpp#L231)。设备初始化时会自动加载并应用已有校准（见 4.2 节消费者）。若不禁用，测得的镜像里已经混入了旧校正的影响，无法得到干净的「裸」失衡数据，新校准会叠在旧校准上失真。

**练习 2**：搜索算法为什么用「逐步缩小区间」而不是一次性细扫整个空间？

**答案**：一次性在 `(phase, ampl)` 全空间以 `precision` 细度扫描，次数会爆炸（步长每缩 1/5，点数平方增长）。多级网格搜索先用粗网格定位最优区域，再在该区域细化（见 [uhd_cal_tx_iq_balance.cpp:246-252](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_cal_tx_iq_balance.cpp#L246-L252)），在保证精度的同时大幅减少测量次数——而每次测量都要发波、收样本，非常耗时。

---

## 5. 综合实践

把四个模块串起来，完成本讲指定的综合实践任务：**说明四类校准数据分别修正何种失真，并指出其 FlatBuffers 源文件**，再补全它们在系统中的存取路径。

**任务**：

1. **失真↔类型↔模式映射表**（填写并核对）：

   | 失真 | 校准类 | `.fbs` 模式 | 修正什么 |
   |------|--------|------------|---------|
   | I/Q 增益与相位不均衡（镜像） | `iq_cal` | `iq_cal.fbs` | ___（A）___ |
   | 宽带多抽头 IQ 失衡 + DC 偏移 | `iq_dc_cal` | `iq_dc_cal.fbs` | ___（B）___ |
   | ZBX 步进衰减器增益不准 | `zbx_tx/rx_dsa_cal` | `dsa_cal.fbs` | ___（C）___ |
   | 绝对输出功率不准 | `pwr_cal` | `pwr_cal.fbs` | ___（D）___ |

2. **追踪一条数据链**：任选一类（建议 IQ），从「**产生**」（哪个工具 / 哪个 `.cpp` 调 `set_cal_coeff`）→「**持久化**」（`database::write_cal_data` 或 RC 嵌入）→「**加载**」（`apply_corrections.cpp` 或 `pwr_cal_mgr.cpp` / `zbx_dboard_init.cpp` 的 `has_cal_data`+`read_cal_data`+`container::make<T>`）→「**消费**」（`get_cal_coeff` / `get_power` / `get_dsa_setting`），画出完整时序。

3. **优先级实验**（源码阅读型）：假设同一 `(key, serial)` 同时存在于 RC、FLASH、FILESYSTEM 三个源，解释 `read_cal_data(key, serial, source::ANY)` 会返回哪一个、为什么，并指出代码位置。

**参考答案要点**：

- (A) 用每频率一个复数系数补偿 I/Q 增益差与相位差，压低镜像频率成分（image rejection）。
- (B) 用多抽头（FIR 式）向量 + DC 偏移 + 缩放/群时延，补偿**频率相关**的宽带 IQ 失衡与 LO 泄漏/直流。
- (C) 把每个频段、每个增益档映射到精确的衰减器步位，使增益档位准确、单调、可重复。
- (D) 建立 gain(dB)↔power(dBm)（随温度、频率）映射，支持按目标功率反查增益，实现绝对功率控制。
- 链路举例（IQ）：`uhd_cal_tx_iq_balance.cpp` 的 `store_results` 写 → `apply_corrections.cpp` 第 93-96 行读 → `iq_cal::get_cal_coeff` 查。
- 优先级：返回 **FILESYSTEM** 的，因为 [database.cpp:199-203](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/cal/database.cpp#L199-L203) 的 `data_fns` 数组把 FILESYSTEM 排在最前，循环命中即返回。

## 6. 本讲小结

- UHD 用 `container` 抽象基类统一所有校准表的生命周期，`serialize()/deserialize()` + 模板工厂 `make<T>()` 让「类型无关的字节存储」与「类型相关的对象解释」解耦。
- 校准字节流由 FlatBuffers 编码，五个 `.fbs` 模式描述四类专用数据 + 一份公共 `Metadata`；每类带 `file_identifier` 便于识别。
- 四类校准各司其职：`iq_cal` 修镜像、`iq_dc_cal` 修宽带 IQ+DC、`dsa_cal` 修 ZBX 增益档、`pwr_cal` 修绝对功率。
- `cal::database` 用 `(key, serial)` 索引，按 **FILESYSTEM > FLASH > RC** 三源优先级查找；RC 族级通用且忽略 serial，写入只支持 FILESYSTEM 且自动备份旧数据。
- 校准工具 `uhd_cal_*`（共享 `usrp_cal_utils`）用「自发自收 + 多级网格搜索」实测失真、迭代寻优，把最优系数经 `iq_cal::serialize()` 写回数据库，闭环运行时的 `apply_corrections`。
- 消费者统一遵循 `has_cal_data` → `read_cal_data` → `container::make<T>()` 三步把字节还原成对象，再在 tune/设增益时查表补偿。

## 7. 下一步学习建议

- **校准在设备初始化中的真实落点**：阅读 `host/lib/usrp/dboard/zbx/zbx_dboard_init.cpp`（DSA 加载，第 121-138 行）和 `host/lib/usrp/common/pwr_cal_mgr.cpp`（功率校准管理器），看具体子板如何把 `database` 接入自己的前端配置。
- **Python 侧使用校准**：结合 u5-l2 的 Python 绑定，了解 `uhd.cal` 模块如何暴露 `iq_cal`/`pwr_cal`，尝试在 Python 里 `serialize()`/`deserialize()` 一份校准表。
- **FlatBuffers 工具链**：UHD 用 `flatc` 把 `.fbs` 编译成 `*_generated.h`（如 `iq_cal.cpp` 第 8 行 include 的 `iq_cal_generated.h`）。可研究构建系统如何调用 `flatc`，以及如何为自定义校准类型新增一个 `.fbs` 与对应 `container` 子类。
- **测试体系**：结合 u5-l5，把 `cal_database_test.cpp` 放回整个 `host/tests` 上下文，理解校准子系统的回归保障。
