# write_fabric_bitstream 与 fast_configuration

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `write_fabric_bitstream` 命令的全部选项，并能解释 `--format`、`--filter_value`、`--path_only`/`--value_only`/`--trim_path` 等选项各自只在哪种输出格式下生效。
- 描述 `plain_text` 与 `xml` 两种比特流文件的本质差异：前者可直接下载进 FPGA，后者仅供 testbench 生成器（如 CocoTB）使用、**不可直接下载**。
- 说清 `fast_configuration` 的加速原理：它依赖架构里声明的「编程 reset/set 全局端口」，先把所有配置位预置成同一个值，从而在写出时省略掉大量相同位；并能解释为什么 scan_chain 只能省略**开头一段连续位**，而 memory_bank / frame_based 可以省略**任意一行/一个字**。
- 会用 `report_bitstream_distribution` 导出比特流分布报告，并据此判断 fast_configuration 能省多少位。

本讲承接 u7-l3（fabric 级比特流 `FabricBitstream` 已经在 context 里就绪），讲解这条数据**如何落盘成文件**，以及如何让它**更短**。

## 2. 前置知识

- **配置位（config bit）/ 可配置存储器（configurable memory）**：FPGA 上决定电路功能的每一位存储单元。比特流就是「把这些位按某种顺序写进芯片」的指令序列。
- **fabric 比特流（fabric bitstream）**：u7-l3 讲过的 `FabricBitstream`，它是带寻址信息（BL/WL 地址、链顺序等）、与配置协议绑定的最终比特流。本讲处理的输入就是它。
- **全局端口（global port）**：贯穿整个 fabric 的特殊信号，例如全局时钟、全局 set/reset。本讲关心其中带 `is_prog`（编程用）+ `is_reset`/`is_set`（复位/置位）标记的全局端口——它们是 fast_configuration 能成立的前提。
- **配置协议（configuration protocol）**：scan_chain / memory_bank / ql_memory_bank / frame_based / standalone（见 u3-l4）。比特流文件的格式（每一行长什么样）完全由协议决定。
- **`//` 注释行**：plain_text 比特流文件以 `//` 开头的行是给人/工具读的元信息（长度、宽度），真正的位流数据在不含 `//` 的行里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `openfpga/src/base/openfpga_bitstream_command_template.h` | 注册 `write_fabric_bitstream`、`report_bitstream_distribution` 等命令及其选项，是「命令长什么样」的权威定义。 |
| `openfpga/src/base/openfpga_bitstream_template.h` | 命令的执行模板：把 shell 选项翻译成 `BitstreamWriterOption`，校验后分派到文本/XML 写出器。 |
| `openfpga/src/fpga_bitstream/bitstream_writer_options.{h,cpp}` | 写出选项的数据模型 `BitstreamWriterOption`，集中管理格式、过滤、fast config 等全部开关。 |
| `openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp` | plain_text 写出器主体：按协议分派成 6 种不同的文本格式，并在此应用 fast_configuration。 |
| `openfpga/src/fpga_bitstream/write_xml_fabric_bitstream.cpp` | XML 写出器：逐位输出带层级路径与地址的 `<bit>` 元素。 |
| `openfpga/src/fpga_bitstream/fast_configuration.cpp` | fast_configuration 的核心判定逻辑：是否可用、该跳过 0 还是 1。 |
| `openfpga/src/utils/fabric_global_port_info_utils.cpp` | 从 fabric 全局端口里挑出「编程用 reset/set 端口」的工具函数。 |
| `openfpga/src/fpga_bitstream/report_bitstream_distribution.cpp` | 比特流分布报告总入口（fabric 级 + 架构级）。 |
| `openfpga/src/fpga_bitstream/report_fabric_bitstream_distribution.cpp` | 报告里 fabric 级部分：按 region 统计位数。 |

## 4. 核心概念与源码讲解

### 4.1 write_fabric_bitstream 命令与选项模型

#### 4.1.1 概念说明

`write_fabric_bitstream` 是把内存中的 `FabricBitstream`「落盘」的命令。它前面必须已经跑过 `build_fabric_bitstream`（u7-l3），是教科书级依赖链的最后一环：

```
repack → build_architecture_bitstream → build_fabric_bitstream → write_fabric_bitstream
```

这条命令有十来个选项，但选项之间有**适用范围**的约束：有些只对 plain_text 有意义（fast config），有些只对 XML 有意义（filter_value、path/value/trim）。为了避免在写出器里到处判断「用户乱传了不适用选项」，OpenFPGA 把全部选项抽到一个数据模型 `BitstreamWriterOption` 里集中管理、集中校验。

#### 4.1.2 核心流程

```
用户在 shell 输入 write_fabric_bitstream --file ... --format ... [其它选项]
        │
        ▼
① 命令模板 add_write_fabric_bitstream_command_template 定义全部选项（command_template.h）
        │
        ▼
② 执行模板 write_fabric_bitstream_template（bitstream_template.h）
   - 把 shell 选项逐个写进一个 BitstreamWriterOption 对象
   - 调用 bitfile_writer_opt.validate(true) 做冲突校验
        │
        ├── file_type == XML ──► write_fabric_bitstream_to_xml_file(...)
        └── file_type == TEXT ─► write_fabric_bitstream_to_text_file(...)
                                  （fast_configuration 仅在这条分支生效）
```

#### 4.1.3 源码精读

命令的全部选项集中在注册函数里。注意每个选项注释里写明了它的适用格式：

- `--format` 默认 `plain_text`，可选 `xml`：[openfpga_bitstream_command_template.h:198-202](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L198-L202)。
- `--filter_value`、`--path_only`、`--value_only`、`--trim_path` 注释都写了「Only applicable to XML file format」：[openfpga_bitstream_command_template.h:204-223](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L204-L223)。
- `--fast_configuration`：减少要下载的比特流大小：[openfpga_bitstream_command_template.h:225-227](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L225-L227)。
- `--keep_dont_care_bits`、`--wl_decremental_order`：仅对 memory_bank flatten 文本格式有意义：[openfpga_bitstream_command_template.h:229-238](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L229-L238)。

执行模板把 shell 选项翻译成 `BitstreamWriterOption` 并校验，然后按格式分派：[openfpga_bitstream_template.h:148-191](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h#L148-L191)。其中 `validate(true)` 失败就直接返回 `CMD_EXEC_FATAL_ERROR`，L177-191 的 `if/else` 就是 XML 与 TEXT 的分叉点。

`BitstreamWriterOption` 是个很朴素的值对象，但它的**成员分组**正好揭示了选项的适用范围——头文件里用注释把字段分成「Universal / XML-specific / Plain-text options」三组：[bitstream_writer_options.h:79-99](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/bitstream_writer_options.h#L79-L99)。可以看到 `fast_config_`、`keep_dont_care_bits_`、`wl_decremental_order_` 都在「Plain-text options」组下——这就是「fast config 只对文本格式生效」在数据结构上的依据。格式枚举只有两种：`enum class e_bitfile_type { TEXT, XML, NUM_TYPES }`（[bitstream_writer_options.h:19](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/bitstream_writer_options.h#L19)），字符串映射表是 `{"plain_text", "xml"}`（[bitstream_writer_options.cpp:17](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/bitstream_writer_options.cpp#L17)）。

校验函数 `validate` 负责拦下互斥/非法组合：[bitstream_writer_options.cpp:126-153](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/bitstream_writer_options.cpp#L126-L153)。两个关键检查：XML 下 `path_only` 与 `value_only` 不能同时为真（L138-143）；`filter_value` 只接受 `0`/`1`（L144-150）。

#### 4.1.4 代码实践

1. **实践目标**：从命令定义反推「哪些选项是文本专属、哪些是 XML 专属」。
2. **操作步骤**：在仓库里执行 `openfpga --help`（或在交互 shell 里 `help write_fabric_bitstream`），把 `write_fabric_bitstream` 的选项表抄下来；再打开 [openfpga_bitstream_command_template.h:185-258](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L185-L258) 对照每个选项的「Only applicable to ...」注释。
3. **需要观察的现象**：`--help` 输出的选项描述里，文本专属项与 XML 专属项会分别标注。
4. **预期结果**：得到一张三列表格——选项名 / 适用格式 / 一句作用。例如 `--fast_configuration` → plain_text → 跳过相同位；`--filter_value` → XML → 只写出指定值的位。
5. 运行结果：待本地验证（依赖 `openfpga` 已编译，见 u1-l3）。

#### 4.1.5 小练习与答案

- **练习**：如果用户对一个 plain_text 输出同时传了 `--path_only` 和 `--value_only`，会发生什么？
- **答案**：`validate` 会通过（它只在 XML 格式下检查这俩互斥，见 [bitstream_writer_options.cpp:136-143](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/bitstream_writer_options.cpp#L136-L143) 的 `if (file_type_ == XML)`），但这两个选项在文本写出器里根本不被读取，因此**被静默忽略**、不影响输出。这正说明「选项的适用范围」是由写出器是否消费它决定的。

- **练习**：`write_fabric_bitstream` 在 shell 依赖图里依赖哪条命令？为什么不依赖 `report_bitstream_distribution`？
- **答案**：依赖 `build_fabric_bitstream`（见 [openfpga_bitstream_command_template.h:363-368](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L363-L368)）。因为写出器读取的 `FabricBitstream` 由 `build_fabric_bitstream` 产生；它不读写报告，故无依赖关系。

### 4.2 比特流写出格式：plain_text 与 xml

#### 4.2.1 概念说明

`write_fabric_bitstream` 有两种输出格式，但它们**用途完全不同**，不是「同一份数据的两种排版」：

- **plain_text（默认）**：这是**真正可下载进 FPGA fabric** 的比特流。文件里 `//` 行是元信息（长度、宽度），其余行是 0/1（memory_bank flatten 还会出现 `x` don't-care）。
- **xml**：这是**给人或外部工具（如 CocoTB testbench 生成器）读**的结构化描述，每一位带完整的模块层级路径和地址。源码注释明确写了它 **can NOT be directly loaded to the FPGA fabric**。

文件里每一行的具体形态**完全由配置协议决定**：standalone、scan_chain、memory_bank（decoder）、ql_memory_bank（flatten / shift_register）、frame_based 各不相同。文本写出器因此写成一个大 `switch`。

#### 4.2.2 核心流程

文本写出器的总入口先决定要不要应用 fast_configuration，再按协议分派到 6 个静态写出函数：

```
write_fabric_bitstream_to_text_file  (write_text L577)
  ├─ apply_fast_configuration = is_fast_configuration_applicable(...) && options.fast_configuration()
  ├─ 若用户开了 fast config 但不适用 → 打印警告并关闭
  ├─ 若适用 → bit_value_to_skip = find_bit_value_to_skip_for_fast_configuration(...)
  └─ switch (config_protocol.type()):
       STANDALONE     → write_flatten_fabric_bitstream_to_text_file       (纯位流)
       SCAN_CHAIN     → write_config_chain_fabric_bitstream_to_text_file  (区域列)
       MEMORY_BANK    → write_memory_bank_fabric_bitstream_to_text_file   (地址+数据行)
       QL_MEMORY_BANK → 再按 bl/wl 子协议分派 decoder / flatten / shift_register
       FRAME_BASED    → write_frame_based_fabric_bitstream_to_text_file   (地址+数据行)
```

#### 4.2.3 源码精读

**fast config 在文本入口的应用点**：[write_text_fabric_bitstream.cpp:606-619](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L606-L619)。注意三步：① `apply_fast_configuration` 是「可用」与「用户要开」的**逻辑与**；② 若用户开了却不适用，打印 `Disable fast configuration even it is enabled by user` 警告（L609-612）；③ 真正可用时才调用 `find_bit_value_to_skip_for_fast_configuration` 决定跳 0 还是跳 1。

**按协议分派的大 switch**：[write_text_fabric_bitstream.cpp:626-682](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L626-L682)。其中 `CONFIG_MEM_QL_MEMORY_BANK` 分支还会按 BL/WL 子协议二次分派（L636-668）。

每种格式的「头两行」就说明了文件如何解读。下面看两个典型形态：

- **standalone（flatten）**——最简单，把所有位连续打印出来，头部声明总长度：[write_text_fabric_bitstream.cpp:59-66](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L59-L66)。
- **scan_chain**——把每个 region 当成一列，每行输出 `num_regions` 位（每个区域贡献一位），头部声明 `Bitstream width (LSB -> MSB): num_regions`：[write_text_fabric_bitstream.cpp:104-119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L104-L119)。fast config 在这里体现为「从第 `num_bits_to_skip` 位才开始写」（L111），长度相应减去前缀（L105-106）。
- **memory_bank（decoder）**——每一行是一次编程：「BL 地址 + WL 地址 + 数据输入」，头部声明三段宽度：[write_text_fabric_bitstream.cpp:162-193](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L162-L193)。
- **frame_based**——每一行是「地址 + 数据输入」：[write_text_fabric_bitstream.cpp:533-560](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L533-L560)。

> 旁注：ql_memory_bank 的「双 flatten」子协议有一条专门的高速写出路径 `fast_write_memory_bank_flatten_fabric_bitstream_to_text_file`（[write_text_fabric_bitstream.cpp:276-434](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L276-L434)）。源码注释记录了它的来由：在 100K LE 的 FPGA 上，旧函数要 600 秒、内存吃紧，新函数只用 1 秒、4MB。其核心是用 `uint8_t` 字节数组按位打包存储（`data[bl>>3] & (1<<(bl&7))`，见 L357-388），这正是 u7-l3 讲过的 `FabricBitstreamMemoryBank` 紧凑表示的落盘端。

**XML 格式**则完全不同——逐位输出一个 `<bit>` 元素，带 `id`、可选 `value`、可选 `path`（完整层级路径），并按协议附上地址。格式契约写在函数顶部的注释里：[write_xml_fabric_bitstream.cpp:48-69](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_xml_fabric_bitstream.cpp#L48-L69)。构造 `<bit>` 的代码：[write_xml_fabric_bitstream.cpp:80-125](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_xml_fabric_bitstream.cpp#L80-L125)（其中 L80-83 是 `--filter_value` 过滤：命中要跳过的值就不写这一位）。最关键的一句定位在总入口注释：**「It can NOT be directly loaded to the FPGA fabric」「designed to be reused by testbench generators, e.g., CocoTB」**：[write_xml_fabric_bitstream.cpp:274-281](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_xml_fabric_bitstream.cpp#L274-L281)。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到同一份 fabric 比特流在 plain_text 与 xml 下完全不同的形态。
2. **操作步骤**：在 u1-l4 跑通过的 `configuration_chain` 任务结果目录里（用 `goto-task` 进入），找到 `fabric_bitstream.bit`；再用 `openfpga -f` 跑一份 XML 版（或临时改脚本里 `write_fabric_bitstream --format xml --file fabric_bitstream.xml`）。
3. **需要观察的现象**：plain_text 文件以 `// Bitstream length:` / `// Bitstream width:` 开头，后面是紧凑的 0/1 串；XML 文件是 `<fabric_bitstream><region><bit id=.. path=..>...<bl address=..><wl address=..>` 的嵌套结构。
4. **预期结果**：plain_text 可被编程器/bitstream loader 直接消费；XML 体积大得多、带完整路径，便于人工或 CocoTB 解析。
5. 运行结果：待本地验证。

#### 4.2.5 小练习与答案

- **练习**：scan_chain 的 plain_text 文件里，`// Bitstream width` 等于什么？为什么是这个值？
- **答案**：等于 region 数量 `fabric_bitstream.num_regions()`（[write_text_fabric_bitstream.cpp:107-108](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L107-L108)）。因为一条 scan_chain 把多个配置区域并行级联，每个时钟周期同时移入「每个区域一位」，所以每一行有 `num_regions` 列。

- **练习**：为什么说 XML 比特流「不能直接下载」？
- **答案**：它包含的是每一位的**层级路径 + 地址**元数据（见 [write_xml_fabric_bitstream.cpp:98-124](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_xml_fabric_bitstream.cpp#L98-L124)），是给人/工具理解「这个位在哪、值是什么」用的，而不是按芯片协议排好的下载序列；真正的下载序列是 plain_text 那份。

### 4.3 fast_configuration：跳过相同位的加速策略

#### 4.3.1 概念说明

芯片里有成千上万个配置位，全部逐位写一遍很慢。**fast_configuration** 的思路：如果架构里声明了「编程用的全局 reset/set 端口」，那么编程开始时可以先用一条全局信号把**所有配置位一次性预置成同一个值**（全 0 或全 1）；于是比特流里凡是等于这个预置值的位都**不需要再单独写**，直接省掉。

于是核心问题变成两个：

1. **能不能用？**——取决于 fabric 有没有「编程 reset」或「编程 set」全局端口。
2. **该跳 0 还是跳 1？**——看哪种值在比特流里出现得多（多则省得多）；当两者一样多时默认跳 0（用 reset）。

注意：fast_configuration 是**安全**的——它不改变最终电路功能，只是利用了「先把所有位预置，再只写不同位」的等价编程顺序。

#### 4.3.2 核心流程

判定流程（全部在 `fast_configuration.cpp`）：

```
is_fast_configuration_applicable(global_ports):
    prog_reset_ports = 全局端口里 is_prog && is_reset 的
    prog_set_ports   = 全局端口里 is_prog && is_set 的
    若两者都空 → 不适用，警告，返回 false
    否则 → true

find_bit_value_to_skip_for_fast_configuration(...):
    若只有 reset 端口 → 跳 0（返回 false）
    若只有 set 端口   → 跳 1（返回 true）
    若两者都有 → 统计能跳的 0/1 数量，取大者；相等默认跳 0
        - scan_chain: 只数「开头一段连续」的 1 / 0
        - memory_bank / frame_based: 数「全部」0 / 1
```

为什么 scan_chain 只数开头、memory_bank 数全部？这是协议物理特性决定的（见 4.3.3）。

#### 4.3.3 源码精读

**是否可用**：[fast_configuration.cpp:20-37](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fast_configuration.cpp#L20-L37)。先调用 `find_fabric_global_programming_reset_ports` / `find_fabric_global_programming_set_ports` 找端口；两者都空就 `VTR_LOG_WARN(... Fast configuration is not applicable)` 并返回 false。

「编程 reset/set 端口」的定义很关键——必须**同时** `is_prog` 与 `is_reset`（或 `is_set`）：[fabric_global_port_info_utils.cpp:22-42](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/fabric_global_port_info_utils.cpp#L22-L42)（reset）、[fabric_global_port_info_utils.cpp:47-67](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/fabric_global_port_info_utils.cpp#L47-L67)（set）。这给出了一条重要推论：**只有架构里 ccff/memory 模型带这样的端口，fast config 才生效**。

> 对照两个真实架构：
> - 基础 cc 架构 `k4_N4_40nm_cc_openfpga.xml` 的 ccff 模型 `DFF` 只有 `prog_clk`（编程时钟），**没有** reset/set 端口：[k4_N4_40nm_cc_openfpga.xml:143-151](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L143-L151)。因此对它开 fast config 会得到「not applicable」并被静默关闭。
> - `k4_N4_40nm_cc_use_reset_openfpga.xml` 的 ccff 模型 `DFFR` 多了一个 `pReset` 端口，标注 `is_reset="true" is_prog="true"`：[k4_N4_40nm_cc_use_reset_openfpga.xml:143-151](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_use_reset_openfpga.xml#L143-L151)。于是 fast config 可用，且因只有 reset 端口，会跳 0。

**该跳 0 还是跳 1**：[fast_configuration.cpp:46-145](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fast_configuration.cpp#L46-L145)。要点：

- 只有一种端口时直接定（L58-63）：只有 reset→跳 0（false），只有 set→跳 1（true）。
- 两种都有才需要统计。统计方式因协议而异：
  - **scan_chain**（L82-105）：数「开头连续的 1」`num_ones_to_skip` 和「开头连续的 0」`num_zeros_to_skip`。**只能数开头一段**，因为链式移位是顺序进位——一旦遇到不同值的位，后面的位都必须照常移入，无法跳过中间段。
  - **memory_bank / ql_memory_bank / frame_based**（L106-121）：遍历**全部**位，分别累计 0 和 1 的数量。因为每个地址（每行）都是独立编程周期，凡是数据等于预置值的行都可以整行跳过，与位置无关。
- 统计后打印两种选择的省略比例（L128-134），取大者；相等时默认跳 0（L137-142）。

**写出端如何使用 `bit_value_to_skip`**：

- scan_chain：算出开头要跳过多少位 `num_bits_to_skip`，输出时从该位开始、长度相应减去前缀：[write_text_fabric_bitstream.cpp:90-106](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L90-L106)，循环从 `num_bits_to_skip` 起步：[write_text_fabric_bitstream.cpp:111-119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L111-L119)。
- memory_bank（decoder）：逐行判断，若该行数据输入**整行**都等于 `bit_value_to_skip` 就 `continue` 跳过这一编程周期：[write_text_fabric_bitstream.cpp:171-182](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L171-L182)。注释强调「Only all the bits in the din port match the value to be skipped, the programming cycle can be skipped!」。frame_based 同理：[write_text_fabric_bitstream.cpp:539-550](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L539-L550)。

#### 4.3.4 代码实践

1. **实践目标**：对比开启/关闭 fast_configuration 时比特流长度，并理解为什么能省。
2. **操作步骤**（用现成的回归任务，arch 已配好 prog reset 端口）：
   - `source openfpga.sh`
   - `run-task basic_tests/full_testbench/configuration_chain`（任务里 `openfpga_fast_configuration=` 为空 → fast config **关**）
   - `run-task basic_tests/full_testbench/fast_configuration_chain`（任务里 `openfpga_fast_configuration=--fast_configuration` → fast config **开**，arch 为 `k4_N4_40nm_cc_use_reset_openfpga.xml`）
   - `goto-task` 分别进两个结果目录，找到 `fabric_bitstream.bit`。
3. **需要观察的现象**：两个文件的 `// Bitstream length:` 行数值不同，开启 fast config 的那份更短；运行日志里还会打印 `Fast configuration will skip X% (a/b) of configuration bitstream.`。
4. **预期结果**：因 `cc_use_reset` 只有 reset 端口，fast config 跳 0；scan_chain 从开头连续的 0 开始省略，故长度减少。具体省略比例取决于该设计 0 位的连续前缀长度。
5. **进阶**：再跑一遍 `run-task basic_tests/full_testbench/configuration_chain`，把它的 `openfpga_arch_file` 临时换成**基础** `k4_N4_40nm_cc_openfpga.xml`（ccff 无 reset 端口）并强加 `--fast_configuration`，观察日志出现 `None of global reset and set ports are defined for programming purpose. Fast configuration is not applicable` 与 `Disable fast configuration even it is enabled by user`，且比特流长度**不缩短**。
6. 运行结果：待本地验证（具体省略百分比依赖设计本身）。

#### 4.3.5 小练习与答案

- **练习**：为什么 scan_chain 的 fast config 只能省「开头一段连续位」，而 memory_bank 可以省任意一行？
- **答案**：scan_chain 是移位寄存器，位必须**顺序**移入；遇到第一个不等于预置值的位之后，后续位无法跳过，所以只能省前缀连续段（[fast_configuration.cpp:82-105](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fast_configuration.cpp#L82-L105)）。memory_bank / frame_based 的每个地址是**独立**编程周期，数据等于预置值的行可逐行跳过，与位置无关（[fast_configuration.cpp:106-121](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fast_configuration.cpp#L106-L121)）。

- **练习**：若架构同时声明了编程 reset 和 set 端口，且比特流里 0 和 1 数量恰好相等，fast config 会跳哪个？
- **答案**：跳 0（用 reset）。见 [fast_configuration.cpp:137-142](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fast_configuration.cpp#L137-L142)：仅当 `num_ones_to_skip > num_zeros_to_skip` 才跳 1，否则（含相等）跳 0。

- **练习**：基础 cc 架构（DFF 无 reset 端口）下，用户硬开 `--fast_configuration` 会改变比特流吗？
- **答案**：不会。`is_fast_configuration_applicable` 返回 false，`apply_fast_configuration` 为 false（[write_text_fabric_bitstream.cpp:606-612](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L606-L612)），打印警告后照常输出完整比特流。

### 4.4 report_bitstream_distribution：比特流分布报告

#### 4.4.1 概念说明

fast_configuration 能省多少位，取决于比特流里 0/1 的分布。`report_bitstream_distribution` 命令把这份分布导出成一份 XML 报告：fabric 级按配置区域（region）统计每个区域多少位，架构级按配置块的层级（可限制深度）统计。它回答的是「位都花在哪了」，帮助判断某个设计是否值得开 fast config、以及哪部分配置位最密集。

#### 4.4.2 核心流程

```
命令 report_bitstream_distribution --file <xml> [--depth N]
        │
        ▼
report_bitstream_distribution_template：depth 默认 1（限制报告体积）
        │
        ▼
report_bitstream_distribution(fname, bitstream_manager, fabric_bitstream, ...)
   ├─ report_fabric_bitstream_distribution   → 每个 region 的 number_of_bits
   └─ report_architecture_bitstream_distribution → 按块层级统计（受 depth 限制）
```

#### 4.4.3 源码精读

命令选项：`--file/-f`（必填）、`--depth`（最大层级）、`--no_time_stamp`、`--verbose`：[openfpga_bitstream_command_template.h:116-148](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L116-L148)。

执行模板里，`depth` 默认为 1（用来「限制报告体积」），负值报错：[openfpga_bitstream_template.h:265-281](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h#L265-L281)。

报告总入口写一个 `<bitstream_distribution>` 根，下含 fabric 与 architecture 两部分：[report_bitstream_distribution.cpp:79-92](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/report_bitstream_distribution.cpp#L79-L92)。

fabric 级部分很简短——为每个 region 输出 `<region id=.. number_of_bits=..>`：[report_fabric_bitstream_distribution.cpp:32-47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/report_fabric_bitstream_distribution.cpp#L32-L47)。可见 fabric 级只统计「每个区域多少位」，不带 0/1 取值分布；要分析 0/1 比例，需结合架构级报告或 fast_configuration 日志里的 `skip X%` 行。

#### 4.4.4 代码实践

1. **实践目标**：导出一份比特流分布报告，看清位数在 region 间的分布。
2. **操作步骤**：在一个已跑过 `build_architecture_bitstream` 的流程脚本里（或交互 shell 中）追加 `report_bitstream_distribution --file bitstream_distribution.xml --depth 2`。
3. **需要观察的现象**：生成 XML，顶层 `<bitstream_distribution>` 下有 `<regions>`（每个 region 的 `number_of_bits`）和架构级块层级统计。
4. **预期结果**：region 的位数之和等于 fabric 比特流总位数；增大 `--depth` 会让架构级统计更细、文件更大。
5. 运行结果：待本地验证。

#### 4.4.5 小练习与答案

- **练习**：`--depth` 不传时默认是多少？为什么默认不是「无限深」？
- **答案**：默认 1（[openfpga_bitstream_template.h:266-268](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h#L266-L268)）。配置块层级会镜像 fabric 模块层级，深度很大时全展开会让报告巨大，故默认限一层。

- **练习**：fabric 级报告能否直接告诉你「fast config 能省多少 0」？
- **答案**：不能。fabric 级只统计每个 region 的位数（[report_fabric_bitstream_distribution.cpp:41](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/report_fabric_bitstream_distribution.cpp#L41)），不含 0/1 取值。能省多少要开 fast_configuration 后看日志的 `Fast configuration will skip X%` 行。

## 5. 综合实践

把本讲三个主题串起来，做一个「为某设计选择最优 fast config 策略」的小任务。

1. **准备**：`source openfpga.sh` 后依次运行三个回归任务，arch 都带合适的编程端口：
   - `run-task basic_tests/full_testbench/fast_configuration_chain`（scan_chain + reset）
   - `run-task basic_tests/full_testbench/fast_memory_bank`（memory_bank）
   - `run-task basic_tests/full_testbench/fast_configuration_frame`（frame_based）
2. **收集**：用 `goto-task` 进入各自最新结果目录，对每个 `fabric_bitstream.bit`：
   - 读出 `// Bitstream length:`；
   - 从运行日志里摘出 `Fast configuration will skip X% (a/b) ...` 与 `Using reset/set ...` 行。
3. **导出报告**：在其中一个流程脚本末尾加 `report_bitstream_distribution --file dist.xml --depth 2`，确认 region 位数。
4. **分析**：把三个协议的「省略比例」填入下表，解释 scan_chain 的省略比例为何通常明显低于 memory_bank/frame_based（回顾 4.3.5 的「前缀段 vs 任意行」）。

   | 协议 | bitstream length（开/关 fast） | 省略比例 | 跳 0 还是跳 1 |
   | --- | --- | --- | --- |
   | scan_chain |  |  |  |
   | memory_bank |  |  |  |
   | frame_based |  |  |  |

5. **结论**：写一两句话说明「在哪种协议下 fast_configuration 收益最大，为什么」。运行结果：待本地验证。

## 6. 本讲小结

- `write_fabric_bitstream` 是依赖链 `repack → build_architecture_bitstream → build_fabric_bitstream → write_fabric_bitstream` 的最后一环，把 `FabricBitstream` 落盘；全部选项集中在 `BitstreamWriterOption`，并在 `validate` 里校验互斥/非法组合。
- 输出有 `plain_text` 与 `xml` 两种格式：**前者可下载、后者仅供 testbench 工具（如 CocoTB）、不可直接下载**。文本格式每一行的形态由配置协议决定（standalone/scan_chain/memory_bank/frame_based 等各不相同）。
- `fast_configuration` 依赖架构里声明的「编程 reset/set 全局端口」（`is_prog` + `is_reset`/`is_set`）；不可用时静默关闭并告警。基础 cc 架构无此端口，需用 `cc_use_reset` 之类才生效。
- 选择跳 0 还是跳 1，看哪种值能省得多，相等默认跳 0；scan_chain 只能省开头连续段，memory_bank/frame_based 可省任意一行。
- `report_bitstream_distribution` 导出 fabric 级（每 region 位数）+ 架构级（按块层级，受 `--depth` 默认 1 限制）分布报告；fast config 实际省略比例要看运行日志的 `skip X%` 行。

## 7. 下一步学习建议

- 想理解 fabric 比特流里 BL/WL 地址和紧凑 `datas/masks` 表示是怎么算出来的，回到 u7-l3（`build_fabric_bitstream` 与 `FabricBitstreamMemoryBank`）。
- 想看 fast_configuration 跳过的位如何反映到仿真验证，进入 u8-l2（testbench 生成）：`write_full_testbench` 会消费这份缩短后的比特流并模拟编程阶段。
- 想了解移位寄存器 bank 这种 memory_bank 高级变体如何在写出端落地，预习 u9-l1（存储器组与移位寄存器 Bank），它解释了本讲 `write_memory_bank_shift_register_fabric_bitstream_to_text_file` 依赖的 `blwl_sr_banks` 数据来源。
