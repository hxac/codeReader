# 厂商 FIFO 封装（Xilinx xpm / Intel scfifo）

## 1. 本讲目标

在 u9-l1 里，我们已经精读了 `fifo_sync` 的厂商无关行为级实现 `own_behavioural_sync_fifo`——它用读写指针与填充水位，把通用的 `dual_clock_dual_port_ram` 拼成了一个 FIFO。本讲转到同一 entity 的另外两套架构：

- `xilinx_behavioural_sync_fifo`：封装 Xilinx 的 `xpm_fifo_sync` 宏。
- `intel_behavioural_sync_fifo`：封装 Intel 的 `scfifo` 宏。

学完本讲，你应当能够：

1. 读懂 `xpm_fifo_sync` 那一长串 generic 与端口是如何映射到本库 entity 端口的。
2. 读懂 Intel `scfifo` 的 `lpm_*` 参数与 `usedw` 计数口径。
3. 把厂商返回的 `wr_data_count` / `usedw` 计数转换成本库统一的 `natural words_stored`。
4. 解释为什么大量厂商输出信号被接到 `*_unconnected`，以及 `read_data_valid` 在两家厂商里为何获取方式完全不同。

## 2. 前置知识

本讲默认你已经掌握以下内容（这些在前面讲义里讲过，这里只引用不重复）：

- **同一 entity 多架构模式**（u2-l1）：一个 entity 声明一次端口契约，配多套 architecture，用 `entity work.fifo_sync(arch_name)` 选定实现；厂商库声明 `library xpm;` / `library altera_mf;` 紧贴各自 architecture 之前，使厂商依赖局部化。
- **厂商仿真库**（u2-l2）：`xpm` 是 Xilinx 参数化宏（封装 `unisim` 原语并带约束），`altera_mf` 是 Intel megafunction；RTL 只负责例化，真实行为由厂商库提供。
- **`to_bits` 函数**（u3-l2）：`to_bits(n)` 返回表示自然数 n 所需的最少位数，约等于 \(\lceil \log_2(n+1) \rceil\)；其具体实现位于 `ip/vhdl_utils` 子模块（待确认）。
- **行为级 FIFO**（u9-l1）：满空标志由填充水位 `fifo_fill_level` 决定，写/读请求先被满空屏蔽成 `fifo_write_request`/`fifo_read_request`，存储体复用 `dual_clock_dual_port_ram`。

此外需要两个外部知识点：

- **xpm_fifo_sync**：Xilinx 提供的「同步时钟 FIFO」宏，单时钟（`wr_clk`/`rd_clk` 共用一个），内含 BRAM 推断、满空标志、数据计数、ECC、可编程阈值等全套功能，端口极多。
- **scfifo**（Single-Clock FIFO）：Intel/Altera 的单时钟 FIFO megafunction，由 `lpm_width`、`lpm_numwords`、`lpm_widthu` 等 `lpm_*` 参数配置，端口比 xpm 精简很多。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) | 三套 architecture 同处一文件：`xilinx_behavioural_sync_fifo`、`intel_behaviourral_sync_fifo`、`own_behavioural_sync_fifo`。本讲聚焦前两套。 |
| [tb/tb_fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd) | 测试台，同时例化 Xilinx 与 own 两套做对照（Intel 未被例化），用于本讲实践。 |

> 提醒：本讲引用的所有行号、所有永久链接都基于当前 HEAD `45eae77`。

## 4. 核心概念与源码讲解

### 4.1 厂商封装的必要性与共享 entity 契约

#### 4.1.1 概念说明

u9-l1 的 `own_behavioural_sync_fifo` 是「厂商无关」的——它用 VHDL 行为级代码手写了整套指针与水位逻辑。它的好处是可开箱仿真、不依赖任何厂商库；代价是性能与资源利用率取决于综合工具对这段 RTL 的推断结果。

而在真实 FPGA 项目里，一旦目标器件确定（例如确定用 Xilinx UltraScale 或 Intel Cyclone V），你通常更愿意直接调用厂商已经深度优化好的 FIFO 硬核宏：

- 它们直接映射到片上 BRAM/Distributed RAM，时序与面积都最优。
- 它们内部已经处理好满空标志的边沿情况、读写冒险、可选 ECC 等「坑」。

于是本库用 u2-l1 讲过的「同一 entity 多架构」模式来兼顾两者：**端口契约（entity）保持不变，内部按需换成厂商宏**。上层模块（例如 `spi_interface`）只要写一次端口连线，换厂商时只改例化时的 `arch_name` 即可。

#### 4.1.2 核心流程

三套架构对同一个 entity 契约负责，但「实现来源」截然不同：

```
                    fifo_sync (entity: 端口契约)
                   /            |              \
   own_behavioural_sync_fifo   xilinx_behavioural_sync_fifo   intel_behavioural_sync_fifo
        |                              |                                |
  手写指针 + 水位               例化 xpm_fifo_sync                  例化 scfifo
  + 复用 dual_clock_             (Xilinx xpm 宏)                    (Intel altera_mf 宏)
    dual_port_ram
```

- **own**：用 `dual_clock_dual_port_ram` 作存储底座，自己算满空（u9-l1 已讲）。
- **xilinx / intel**：**不再**例化 `dual_clock_dual_port_ram`，而是把整个 FIFO 一次性替换成一个厂商黑盒宏。这是厂商封装与行为级实现最根本的结构差异。

#### 4.1.3 源码精读：共享的 entity 契约

三套架构共用同一个 entity。先看清这份契约，后面两节的映射才有对照基准：

[fifo_sync.vhd:7-25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L7-L25) —— entity 声明：3 个 generic + 10 个端口。

关键点（本讲后续反复用到）：

- `write_data` / `read_data` 是**非约束** `std_ulogic_vector`，位宽推迟到例化时由外部连线决定；厂商架构里靠 `write_data'length` / `read_data'length` 把位宽喂给厂商 generic。
- `words_stored : natural range 0 to FIFO_DEPTH`——这是一个**带上下界的整型**输出，本库统一的计数口径；厂商宏返回的是位向量，必须转换并确保不越界。
- 三个 generic 里有**两个其实是「架构专属」**的：`UNDER_AND_OVERFLOW_ASSERTIONS` 只被 `own` 用，`INTEL_DEVICE_FAMILY` 只被 `intel` 用。这是把厂商相关配置「上浮」到共享 entity 的务实写法，代价是换架构时某些 generic 会失效——值得留意。

#### 4.1.4 代码实践（阅读型）

1. **目标**：建立「一份 entity、三套实现」的全局视图。
2. **步骤**：打开 [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd)，定位三个 `architecture ... of fifo_sync is` 关键字（分别在 L35、L107、L147）。
3. **观察**：每套 architecture 之前紧贴的 `library`/`use` 声明——`xilinx` 前是 `library xpm`，`intel` 前是 `library altera_mf`，`own` 前没有任何厂商库。
4. **预期**：能复述「厂商依赖局部化」在源码里的体现，并指出 `own` 架构正是靠「无厂商库声明」来证明自己厂商无关。

#### 4.1.5 小练习与答案

**练习**：entity 里的 `INTEL_DEVICE_FAMILY` 默认值是什么？为什么它出现在共享 entity 而不是只出现在 Intel architecture 内部？

**参考答案**：默认 `"Cyclone V"`（见 L11）。它出现在共享 entity 是因为 VHDL 的 generic 必须声明在 entity 上、不能声明在 architecture 上；想让 Intel architecture 能接收一个「目标器件族」字符串，就只能把它上浮到 entity。副作用是：选 `own` 或 `xilinx` 架构时，这个 generic 即使被外部赋值也不会产生任何效果。

---

### 4.2 Xilinx xpm_fifo_sync 封装（xilinx_behavioural_sync_fifo）

#### 4.2.1 概念说明

`xpm_fifo_sync` 是 Xilinx 提供的同步 FIFO 宏，功能极其完整：满/空、可编程阈值（prog_full/prog_empty）、读写数据计数、几乎满/几乎空、上溢/下溢指示、复位忙指示、ECC 错误注入与指示……端口有二十多个。本库**只需要其中一小部分**，封装的工作就是：

1. 把本库的简单端口名映射到 xpm 的端口名；
2. 给一组合理的默认 generic；
3. 把本库不需要的 xpm 输出接到「悬空信号」，让综合器满意。

#### 4.2.2 核心流程

封装的信号流如下（左侧是本库 entity 端口，右侧是 xpm 端口）：

```
sys_clk        ───────────────────► wr_clk          （同步 FIFO 单时钟）
sys_rst_n      ──[not 取反]────────► rst             （xpm 复位高有效）
write_enable   ───────────────────► wr_en
write_data     ───────────────────► din
read_enable    ───────────────────► rd_en
read_data      ◄─────────────────── dout
full           ◄─────────────────── full
empty          ◄─────────────────── empty
read_data_valid◄─────────────────── data_valid      （xpm 直接提供！）
words_stored   ◄──[to_integer(unsigned(wr_data_count))]
                                     wr_data_count
```

注意复位那一行：xpm 的 `rst` 是**高有效**，而本库约定 `sys_rst_n` 是**低有效**（看 own 架构里 `if sys_rst_n = '0'` 即复位），所以封装里写了 `rst => not sys_rst_n` 做极性翻转。这是厂商封装最典型的「适配细节」。

#### 4.2.3 源码精读

先看架构前的厂商库声明与本架构局部信号：

[fifo_sync.vhd:27-36](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L27-L36) —— `library xpm; use xpm.vcomponents.all;` 紧贴 xilinx 架构之前；本架构只声明一个「真用到的」中间信号 `wr_data_count`（用于推导 `words_stored`）。

接下来是 12 个「悬空信号」声明：

[fifo_sync.vhd:40-51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L40-L51) —— `prog_full_unconnected`、`rd_data_count_unconnected`、`wr_ack_unconnected`、`overflow_unconnected`…… 这些信号声明了却从不被读取，唯一作用是「接住」xpm 的多余输出端口（详见 4.4 节）。

再看 generic 映射（只摘关键几行）：

[fifo_sync.vhd:55-74](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L55-L74) —— 注意几个用属性「反向取值」的 generic：

- `FIFO_WRITE_DEPTH => FIFO_DEPTH`：FIFO 深度直接透传。
- `WRITE_DATA_WIDTH => write_data'length`：从非约束端口的实际位宽反推。
- `WR_DATA_COUNT_WIDTH => wr_data_count'length`：让 xpm 的写计数位宽与本地信号一致。
- `RD_DATA_COUNT_WIDTH => rd_data_count_unconnected'length`：因为读计数不用，故意只给 1 位（信号声明为 `std_ulogic_vector(0 downto 0)`），最小化开销。

最后是端口映射里几处最值得注意的：

[fifo_sync.vhd:75-101](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L75-L101) —— `rst => not sys_rst_n`（极性翻转）、`wr_clk => sys_clk`（同步 FIFO 单时钟）、`data_valid => read_data_valid`（**直接拿到**数据有效指示，这是 xpm 相对 scfifo 的一大便利）。

以及 `words_stored` 的转换（架构体的第一行并发语句）：

[fifo_sync.vhd:53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L53) —— `words_stored <= to_integer(unsigned(wr_data_count));` 把 xpm 的写侧计数控向量转成自然数。注意此处**没有**显式钳位，依赖 xpm 的 `wr_data_count` 自然不会超过 `FIFO_DEPTH`。

#### 4.2.4 代码实践（阅读 + 推理型）

1. **目标**：搞清 xpm 架构里每个 entity 输出「由哪个 xpm 信号驱动」。
2. **步骤**：对照上面 L75-L101 的端口映射，填一张表（entity 端口 → 驱动它的 xpm 端口/逻辑）。
3. **观察**：`full`、`empty`、`read_data`、`read_data_valid` 都是「一根线直连」xpm；只有 `words_stored` 多了一步 `to_integer(unsigned(...))` 转换。
4. **预期结果**：你能指出「xpm 架构几乎不含任何手写时序逻辑，全部行为来自黑盒」这一结构事实。
5. **待本地验证**：若本地装了 Vivado/xpm 库，可把测试台里被注释掉的 `check_equal(..., msg => "full_xilinx")` 等断言（见 tb L125/L129/L135）打开，观察 xpm 的满空时序是否与 own 完全一致——通常会有「早一拍/晚一拍」的细微差异，这正是测试台默认注释掉 xilinx 检查的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RD_DATA_COUNT_WIDTH` 要写成 `rd_data_count_unconnected'length` 而不是直接写常数？

**参考答案**：让 generic 与对应信号的声明「同源」，改一处即可。`rd_data_count_unconnected` 被声明成 1 位向量，`'length` 取出来就是 1；这样既满足 xpm「必须给宽度」的要求，又把不用的读计数开销压到最小，且不会有「信号 3 位、generic 写 2 位」的不一致隐患。

**练习 2**：`words_stored` 是 `natural range 0 to FIFO_DEPTH`，但 `to_integer(unsigned(wr_data_count))` 没做钳位，这安全吗？

**参考答案**：在 xpm 行为正确的前提下是安全的——`wr_data_count` 表示 FIFO 内现存字数，最多等于深度 `FIFO_DEPTH`，恰落在 `0 to FIFO_DEPTH` 内。若担心 xpm 在边界上瞬时报告越界值，可像 Intel 架构那样加一道 `when not full else ...'subtype'high` 钳位（见 4.3）。

---

### 4.3 Intel scfifo 封装（intel_behavioural_sync_fifo）

#### 4.3.1 概念说明

Intel 的 `scfifo`（Single-Clock FIFO）比 xpm 精简得多：端口只有 `clock`、`data`、`wrreq`、`rdreq`、`full`、`empty`、`q`、`usedw`、`sclr`、`aclr` 等十来个，几乎没有「多余输出」。它用一组 `lpm_*`（LPM = Library of Parameterized Modules）参数来配置宽度、深度、计数位宽。

精简带来一个直接后果：**scfifo 没有 `data_valid` 输出**。而本库 entity 明确要求输出 `read_data_valid`，于是封装必须自己「造」一个——这是 Intel 封装比 Xilinx 多出的一行手写逻辑。

#### 4.3.2 核心流程

```
sys_clk        ───────────────────► clock            （单时钟）
sys_rst_n      ───────────────────► sclr             （见 4.3.4 的极性讨论）
write_data     ───────────────────► data
write_enable   ───────────────────► wrreq
read_enable    ───────────────────► rdreq
read_data      ◄─────────────────── q
full           ◄─────────────────── full
empty          ◄─────────────────── empty
                                    usedw ──► words_stored_slv ──► words_stored
read_data_valid◄──[read_enable when rising_edge(sys_clk)]   （自己造！）
```

#### 4.3.3 源码精读

厂商库声明与本架构局部信号：

[fifo_sync.vhd:104-108](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L104-L108) —— `library altera_mf; use altera_mf.altera_mf_components.all;`；本架构只需一个中间信号 `words_stored_slv`（接 `usedw`），**没有任何 `*_unconnected` 悬空信号**——因为 scfifo 本来就没什么多余输出。

scfifo 的 generic 映射（LPM 参数）：

[fifo_sync.vhd:110-125](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L110-L125) —— 关键 LPM 参数：

- `lpm_width => write_data'length`：数据位宽。
- `lpm_numwords => FIFO_DEPTH`：FIFO 深度（字数）。
- `lpm_widthu => to_bits(FIFO_DEPTH)`：`usedw` 计数的位宽。
- `add_ram_output_register => "ON"`：读出寄存一拍（对应 1 拍读延迟，与 `read_data_valid` 的推导相配）。
- `intended_device_family => INTEL_DEVICE_FAMILY`：把 entity 上浮来的器件族字符串喂给 Quartus。
- `use_eab => "ON"`：用 EAB（嵌入式阵列块）/RAM 实现，而非寄存器堆。
- `overflow_checking` / `underflow_checking => "ON"`：满写、空读由 scfifo 内部忽略。

端口映射：

[fifo_sync.vhd:126-136](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L126-L136) —— `clock => sys_clk`、`sclr => sys_rst_n`、`q => read_data`、`usedw => words_stored_slv`，端口一一对应，没有悬空。

`read_data_valid` 与 `words_stored` 的手写转换（这是 Intel 封装比 Xilinx 多出的两行）：

[fifo_sync.vhd:138-139](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L138-L139) ——

- 第 138 行：`read_data_valid <= read_enable when rising_edge(sys_clk);` 由于 scfifo 不提供 `data_valid`，封装把 `read_enable` 寄存一拍当作有效指示（依赖 `add_ram_output_register => "ON"` 带来的 1 拍读延迟）。
- 第 139 行：`words_stored <= to_integer(unsigned(words_stored_slv)) when not full else words_stored'subtype'high;` 把 `usedw` 转自然数，并在 `full` 时**显式钳位**到 `words_stored'subtype'high`（即 `FIFO_DEPTH`）。

#### 4.3.4 代码实践（推理 + 待验证型）

1. **目标**：理解 scfifo 封装里两处「手写补偿」的来历，并发现一处值得复核的极性细节。
2. **步骤**：
   - 对比 Xilinx 架构，确认 Intel 多写了哪两行逻辑（答案：`read_data_valid` 与 `words_stored` 钳位）。
   - 比较复位映射：Xilinx 写 `rst => not sys_rst_n`，Intel 写 `sclr => sys_rst_n`（**没有** `not`）。
3. **需要思考的现象**：`scfifo` 的 `sclr` 是**高有效**同步清零，而本库 `sys_rst_n` 按命名与 `own` 架构（`if sys_rst_n = '0'` 复位）的约定是**低有效**。把低有效信号直接接高有效端口，极性是否一致？
4. **预期结果**：你能指出「Xilinx 封装做了极性翻转、Intel 封装没做」这一不对称，并能说出它对复位行为可能的影响。
5. **待本地验证**：本仓库的 `tb_fifo_sync` **并未例化** Intel 架构（见 tb L570-L602 只例化了 `DuT_xilinx` 与 `DuT_own`），所以这一极性细节在现有测试里无法被捕获。若你有 Quartus/ModelSim Intel 版环境，建议自行例化 `intel_behavioural_sync_fifo`，在复位期间观察 `empty` 是否如期拉高、`usedw` 是否清零，以确认行为是否符合预期。

> 说明：以上仅基于源码静态阅读提出观察点，不替你下「是 bug」的结论——厂商宏在某些配置下对 sclr 的解释可能另有约定，最终以本地带 Intel 库的仿真为准。

#### 4.3.5 小练习与答案

**练习**：`lpm_widthu => to_bits(FIFO_DEPTH)`，而 Xilinx 侧写计数信号用的是 `to_bits(FIFO_DEPTH) - 1 downto 0`，两者矛盾吗？

**参考答案**：不矛盾。`to_bits(FIFO_DEPTH)` 返回「表示深度值所需位数」\(W\)；`std_ulogic_vector(W - 1 downto 0)` 正好是 \(W\) 位向量。本架构里 `words_stored_slv` 的位宽与 `lpm_widthu` 取的都是同一个 \(W\)，二者一致。

---

### 4.4 悬空信号处理与 words_stored 计数口径转换

#### 4.4.1 概念说明

本节把 4.2、4.3 两条线索汇成两张对照表，直接回答本讲的核心实践任务：

1. **四个关键输出（full / empty / words_stored / read_data_valid）各由哪个厂商信号驱动？**
2. **为什么 xpm 有那么多 `*_unconnected`，而 scfifo 一个都没有？**

「悬空信号」的本质：厂商宏的端口是「最大超集」，本库只挑需要的子集；VHDL 不允许端口不连，于是把不用的输出接一个「声明了却从不读取」的本地信号，让综合器在优化阶段自然把它删掉。这就是 `*_unconnected` 命名的由来。

#### 4.4.2 核心流程

`words_stored` 的计数位宽由深度决定。设深度为 \(D\)，则计数值范围是 \(0 \ldots D\)（注意是 \(D+1\) 个可能值，因为「正好满」也要能表示），所需位数为：

\[
W = \lceil \log_2(D+1) \rceil \approx \text{to\_bits}(D)
\]

两套厂商架构都把宽度取为 `to_bits(FIFO_DEPTH)` 位（Xilinx 的 `wr_data_count`、Intel 的 `words_stored_slv` 声明都是 `to_bits(FIFO_DEPTH) - 1 downto 0`，即 \(W\) 位）。差别只在「是否在满时钳位」：

| 架构 | words_stored 表达式 | 满时是否钳位 |
|------|---------------------|--------------|
| own（u9-l1） | `FIFO_DEPTH when full else to_integer(fifo_fill_level)` | 是 |
| xilinx | `to_integer(unsigned(wr_data_count))` | 否（依赖 xpm 不越界） |
| intel | `to_integer(unsigned(words_stored_slv)) when not full else words_stored'subtype'high` | 是 |

`words_stored'subtype'high` 这种写法值得学：`words_stored` 声明为 `natural range 0 to FIFO_DEPTH`，故 `'subtype'high` 就是 `FIFO_DEPTH`，既避免硬编码常数，又保证钳位值与 entity 契约的上界永远一致。

#### 4.4.3 源码精读：四个输出的驱动来源对照

下表把四个 entity 输出在三套架构里的驱动来源一次列清（行号均指向 `fifo_sync.vhd`）：

| entity 输出 | own_behavioural | xilinx_behavioural | intel_behavioural |
|-------------|-----------------|--------------------|-------------------|
| `full` | `fifo_fill_level >= FIFO_DEPTH`（[L179](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L179)） | xpm `full` 直连（[L81](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L81)） | scfifo `full` 直连（[L131](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L131)） |
| `empty` | `fifo_fill_level = 0`（[L180](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L180)） | xpm `empty` 直连（[L90](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L90)） | scfifo `empty` 直连（[L134](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L134)） |
| `words_stored` | `FIFO_DEPTH when full else to_integer(fifo_fill_level)`（[L216](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L216)） | `to_integer(unsigned(wr_data_count))`（[L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L53)） | `to_integer(unsigned(words_stored_slv)) when not full else ...'subtype'high`（[L139](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L139)） |
| `read_data_valid` | `read_enable and not empty`（寄存，[L226](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L226)） | xpm `data_valid` 直连（[L96](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L96)） | `read_enable when rising_edge(sys_clk)`（手写，[L138](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L138)） |

最值得品味的是 `read_data_valid` 这一行——三家各不相同：

- **own**：`read_enable and not empty`，显式屏蔽了「空读」，语义最严谨。
- **xilinx**：厂商直接给 `data_valid`，免费且正确。
- **intel**：只能手写 `read_enable` 寄存一拍，**没有 `and not empty`**——因为 scfifo 内部 `underflow_checking => "ON"` 已会忽略空读，但封装这一行在「空读」时仍会拉高 `read_data_valid`，这是 Intel 封装的又一处可观察差异。

再看 `*_unconnected` 的全貌：

[fifo_sync.vhd:40-51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L40-L51) —— Xilinx 架构声明了 12 个悬空信号：`prog_full_unconnected`、`prog_empty_unconnected`、`rd_data_count_unconnected`、`wr_ack_unconnected`、`overflow_unconnected`、`underflow_unconnected`、`wr_rst_busy_unconnected`、`rd_rst_busy_unconnected`、`almost_full_unconnected`、`almost_empty_unconnected`、`sbiterr_unconnected`、`dbiterr_unconnected`。它们在端口映射里（[L82-L100](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L82-L100)）接住 xpm 的对应输出，却从不被任何进程读取——综合时被当作 dead logic 删除。

对比之下，Intel 架构的端口映射（[L126-L136](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L126-L136)）里**一个 `*_unconnected` 都没有**。原因正是 scfifo 的端口集合本身就是「精简实用子集」，本库几乎用到了全部输出。

#### 4.4.4 代码实践（本讲主实践）

1. **目标**：亲手完成「三套架构 × 四个输出」的驱动来源对照，并解释悬空信号。
2. **操作步骤**：
   - 打开 [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd)，分别在 `xilinx`（L35-L102）、`intel`（L107-L140）、`own`（L147-L241）三段里追踪 `full`、`empty`、`words_stored`、`read_data_valid` 这四个输出各自由哪条语句驱动。
   - 把结果填入一张 4×3 的表（可直接参照 4.4.3 那张表来核对）。
   - 数一数 xilinx 段里 `*_unconnected` 信号的数量，并在端口映射里逐个确认它们「只被赋值、从不被读」。
3. **需要观察的现象**：
   - `read_data_valid` 在 Intel 是手写一行寄存，在 Xilinx 是厂商直给，在 own 是带 `not empty` 屏蔽——三种来源。
   - `words_stored` 在 Intel/own 有满时钳位，在 Xilinx 没有。
4. **预期结果**：你能不查讲义、独立说出每个输出的驱动来源，并用一句话解释「xpm 端口是超集，scfifo 端口是精简集，所以前者需要 `*_unconnected`、后者不需要」。
5. **加分项（待本地验证）**：在 `tb_fifo_sync.vhd` 中新增一个 `DuT_intel` 例化（参照 [L570-L602](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L570-L602) 的写法，改 `arch_name` 为 `intel_behavioural_sync_fifo`），把现有对 own 的 `check_equal` 复制一份指向 Intel 实例，跑一次仿真看 Intel 封装是否能通过全部用例——这会直接暴露 4.3.4 提到的复位极性与 `read_data_valid` 差异。需要 Intel 厂商库支持。

#### 4.4.5 小练习与答案

**练习 1**：如果有一天你想用上 xpm 的「几乎满」预警（`almost_full`），需要改哪两处？

**参考答案**：① 把 `almost_full_unconnected` 改成一个真正对外引出的端口（或在架构内消费它）；② 在 entity 上新增一个对应的输出端口（例如 `almost_full`），并在 xilinx 架构里连上。其它两套架构也要补上同名端口的占位实现，以维持端口契约一致。

**练习 2**：为什么 `words_stored` 的子类型上界用 `words_stored'subtype'high` 引用，而不是直接写 `FIFO_DEPTH`？

**参考答案**：`words_stored` 已声明为 `natural range 0 to FIFO_DEPTH`，用 `'subtype'high` 取上界能自动跟随 entity 上对 `FIFO_DEPTH` 的修改，避免「entity 改了深度、架构里钳位常数却忘改」的不一致；这是把「单一真相源」贯彻到钳位逻辑的小技巧。

## 5. 综合实践

把本讲的三套架构当作「同一个 FIFO 的三种实现」，做一次跨实现等价性核查：

1. **阅读准备**：通读 [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) 三套 architecture，确认它们端口契约完全一致、内部实现互不相同。
2. **填表**：独立完成 4.4.4 的「4 输出 × 3 架构」驱动来源表（先不看答案）。
3. **画时序**：针对「复位后写 1 个字 → 读 1 个字」这一最小场景，分别画出 own、xilinx、intel 三套实现的 `read_enable`、`read_data_valid`、`read_data`、`empty` 时序草图，标注哪套的 `read_data_valid` 可能在「空读」时仍拉高。
4. **写一段适配说明**：假设你要把这个 FIFO 从 Xilinx 平台迁到 Intel 平台，列出「需要改的例化语法」与「可能行为不一致、需要重新验证的点」各一条。
5. **预期产出**：一张对照表 + 一份时序草图 + 一段迁移说明。涉及厂商库仿真的部分若本地无法运行，标注「待本地验证」即可，不要假装跑过。

这个任务串起了本讲全部要点：entity 契约的统一性、generic/端口映射、计数口径转换、`read_data_valid` 的厂商差异、以及悬空信号的存在意义。

## 6. 本讲小结

- `fifo_sync` 用「同一 entity 多架构」为同一份端口契约提供三套实现：`own`（行为级）、`xilinx`（xpm_fifo_sync）、`intel`（scfifo）。
- 厂商架构**不再**例化 `dual_clock_dual_port_ram`，而是把整个 FIFO 替换成一个厂商黑盒宏——这是与行为级实现最根本的结构差异。
- Xilinx 侧做复位极性翻转（`rst => not sys_rst_n`）、用属性反推位宽、并把 12 个不用的 xpm 输出接到 `*_unconnected` 让综合器删除。
- Intel 侧 scfifo 端口精简、无悬空信号；但因 scfifo 不提供 `data_valid`，封装多写了 `read_data_valid <= read_enable when rising_edge(sys_clk)`，并对 `words_stored` 做了满时钳位。
- `words_stored` 把厂商的位向量计数（`wr_data_count` / `usedw`）经 `to_integer(unsigned(...))` 转成本库统一的 `natural`；位宽取 `to_bits(FIFO_DEPTH)`，约等于 \(\lceil \log_2(D+1) \rceil\)。
- 现有测试台只例化并严格校验了 `own` 架构（xilinx 的断言被注释、intel 未例化），所以厂商封装的时序细节主要靠带厂商库的本地仿真验证。

## 7. 下一步学习建议

- 下一讲 **u9-l3（异步 FIFO 与格雷码指针）** 会把 FIFO 推进到跨时钟域：讲解 `fifo_async` 的 `own_behavioural_async_fifo` 如何用二进制↔格雷码转换 + `ff_synchroniser_vector` 跨域同步指针。你在本讲看到的「封装替换存储底座」思路在那里依然适用，但会多出指针同步这一层。
- 建议同步回顾 **u8-l2（多比特同步器）**：`fifo_async` 正是用它来同步格雷码指针的，理解它对读懂异步 FIFO 必不可少。
- 若想加深对厂商宏本身的认识，可阅读 Xilinx UG974（xpm 文档）与 Intel Quartus Prime Design Suite 的 `scfifo` megafunction 指南，对照本讲列出的 generic/端口含义。
