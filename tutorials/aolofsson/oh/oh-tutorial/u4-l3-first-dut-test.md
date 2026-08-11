# 编写你的第一个 DUT 测试

## 1. 本讲目标

本讲要把前两讲（u4-l1 的 `dv_top` 三段式骨架、u4-l2 的 `.emf` 激励格式）拼成一个完整的动作：**把一个你自己的 IP 包装成测试平台认识的 `dut` 模块，再端到端跑一次仿真**。

学完后你应该能够：

- 说出 `dut` 包装的「固定端口契约」有哪些信号，为什么必须是这套。
- 看懂 `/*AUTOINST*/`、`AUTO_TEMPLATE`、`/*AUTOWIRE*/` 这些注释标记到底做了什么、由谁展开。
- 仿照 `dut_gpio.v` 为一个 stdlib 原语（如 `oh_counter`）写出新的 `dut` 包装，并知道如何用 `build.sh` + `sim.sh` 把它跑起来（以及当前仓库脚本里哪些路径需要你先修掉）。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自依赖讲义）：

- **u2-l2 时序原语**：看得懂一个带时钟、复位的 D 触发器/寄存器怎么写；知道 `oh_counter` 这类时序原语的端口长什么样。
- **u4-l1 通用测试平台架构**：记得 `dv_top` 的三段式结构（`dv_ctrl` 控制 + `dut` 被测 + `dv_driver` 驱动与监视），以及 `dut` 模块是在**编译期**由用户提供的 `.v` 文件整体替换的。
- **u4-l2 激励驱动与 .emf 测试格式**：记得 `.emf` 一行一个事务，由 `datahi_datalo_dstaddr_ctrlmode_access` 五个下划线分隔的十六进制段组成；`ctrlmode` 的 bit0 是写位、bits[2:1] 是数据宽度模式；运行时由 `stimulus.v` 用 `$readmemh` 读入并回放。

再补两个本讲会用到、但前面没展开的小概念：

- **emesh 包宽度**：`PW = 2*AW + 40`，当地址宽 `AW=32` 时 `PW=104` 位。这是 `dut` 端口里 `packet_in`/`packet_out` 的基本单位（见 [stdlib/testbench/dv_top.v:5-8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L5-L8)）。
- **verilog-mode（Emacs 插件）**：OH! 大量使用它的 `AUTO*` 系列宏来自动生成端口连接。**关键认知：iverilog 不会执行这些宏**——仓库里的 `.v` 文件已经保存了「展开后」的结果，`/*AUTOINST*/` 等只是标记「这块连接是 verilog-mode 生成的」。详见 4.2。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [gpio/dv/dut_gpio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v) | **本讲主角**：把 `gpio` 这个 IP 包装成标准 `dut` 模块的完整范例，含 `AUTO_TEMPLATE`。 |
| [stdlib/testbench/dut_template.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_template.v) | 空白 `dut` 包装模板（端口契约 + tie-off 占位），是写新 `dut` 的起点。 |
| [stdlib/testbench/dv_top.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v) | 顶层测试平台，它在第 67 行实例化 `dut`——这就是「契约」的来源。 |
| [gpio/dv/tests/test_basic.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/tests/test_basic.emf) | gpio 的端到端激励文件，11 个事务覆盖了 GPIO 的寄存器读写。 |
| [scripts/build.sh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh) | 编译脚本：把 `dut_xxx.v` 编译成 `dut.bin`。 |
| [stdlib/testbench/libs.cmd](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/libs.cmd) | iverilog 的 `-y/+incdir` 库搜索路径清单（**有遗留问题，见 4.3**）。 |
| [stdlib/rtl/oh_counter.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v) | 综合实践要包装的目标原语：参数化计数器。 |

补充对照（同一契约的不同填法，帮助你看清套路）：

- [stdlib/testbench/dut_gray.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_gray.v)：最简单的「环回」包装，适合作为实践的模仿对象。
- [stdlib/testbench/dut_clockdiv.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_clockdiv.v)：从 `packet_in` 取控制位驱动原语。
- [stdlib/testbench/dut_fifo_generic.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_fifo_generic.v)：把 `access/packet/wait` 直接转交给 `oh_fifo_cdc`。

## 4. 核心概念与源码讲解

### 4.1 dut 包装：把任意 IP 塞进固定的外壳

#### 4.1.1 概念说明

回顾 u4-l1：`dv_top` 这个测试平台是**不变的**，它内部实例化了一个名叫 `dut` 的模块。你换一个被测 IP，不需要改 `dv_top`，只需要写一个新的 `dut_xxx.v`，在编译时用它替换掉上次的 `dut` 即可。

这能成立的前提是：**所有 `dut` 包装必须遵守同一套端口契约**。这套契约不是文档约定的，而是 `dv_top` 实例化 `dut` 时写死的——`dv_top` 怎么连，你的 `dut` 端口就得长什么样。

换句话说，`dut` 包装的本质是**一个适配器（adapter）**：左边是固定不变的「测试平台契约」（时钟、复位、emesh 事务通道），右边是你千奇百怪的 IP 接口。包装的工作就是把两边对接起来，接不上的信号做合理兜底（tie-off）。

#### 4.1.2 核心流程

写一个 `dut` 包装的标准步骤：

1. **抄契约**：从模板 `dut_template.v` 复制 `module dut(...)` 的完整端口列表和 `parameter N/PW`（这套端口不能改）。
2. **兜底固定信号**：`dut_active=1`（告诉平台「我已就绪」）、`wait_out=0`（不向上游反压）、`clkout=clk1`（回送一个观察时钟）。
3. **派生内部时钟**：多数 IP 需要一个 `clk`，通常 `assign clk = clk1;`。
4. **实例化你的 IP**：用 `#(.N(...))` 传参，按名连接端口。
5. **桥接事务**：把 `access_in/packet_in` 里的字段喂给 IP，把 IP 的输出塞回 `access_out/packet_out`。

伪代码：

```
module dut( 固定的 10 个端口 );          // 不能改
   参数 N=1, PW=104;                     // 通道数与包宽
   dut_active=1; wait_out=0; clkout=clk1; // 兜底
   clk = clk1;
   你的_IP #( 参数 ) 实例 ( 端口映射 );    // 把 packet_in 字段喂进去，packet_out 字段取出来
endmodule
```

#### 4.1.3 源码精读

先看契约从哪来。`dv_top` 在 [stdlib/testbench/dv_top.v:67-84](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L67-L84) 实例化 `dut`：

```verilog
dut #(.PW(PW), .N(N)) dut (
    .dut_active(dut_active), .clkout(clkout),
    .wait_out(dut_wait[N-1:0]),
    .access_out(dut_access[N-1:0]),
    .packet_out(dut_packet[N*PW-1:0]),
    .clk1(clk1), .clk2(clk2), .nreset(nreset),
    .vdd(vdd[N*N-1:0]), .vss(vss),
    .access_in(stim_access[N-1:0]),
    .packet_in(stim_packet[N*PW-1:0]),
    .wait_in(stim_wait[N-1:0]));
```

这就是契约的「唯一真相」：5 个输出（`dut_active/clkout/wait_out/access_out/packet_out`）+ 7 个输入（`clk1/clk2/nreset/vdd/vss/access_in/packet_in/wait_in`），外加参数 `N`（通道数）和 `PW`（包宽）。注意 `access_in` 等都是 `[N-1:0]`、`packet_in` 是 `[N*PW-1:0]`——这套宽度也是契约的一部分。

再看模板 `dut_template.v` 给出的「待填空壳」，[stdlib/testbench/dut_template.v:1-35](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_template.v#L1-L35) 完整复制了上面的端口，并给出占位 tie-off：

```verilog
assign dut_active = 1'b1;
assign clkout     = clkin1;   // ⚠️ 模板里的笔误：应为 clk1，clkin1 未定义
assign clk        = clkin1;   // ⚠️ 同上；且 clk 未在端口声明，需自己补 wire
```

> **现实提示**：`dut_template.v` 第 32-33 行写的是 `clkin1`，但端口里只有 `clk1`，`clkin1` 未定义——这是模板里的历史笔误。照抄时记得改成 `clk1`，并自己补一句 `wire clk;`。这正是 u1-l1 强调的「代码为事实、文档/模板可能滞后」。

然后看一个「填好了」的真实范例。`dut_gpio.v` 的端口段 [gpio/dv/dut_gpio.v:1-40](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L1-L40) 与契约一字不差，关键在它怎么把 `gpio` 这个 IP 接进来。先看兜底与派生时钟 [gpio/dv/dut_gpio.v:57-60](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L57-L60)：

```verilog
assign wait_out[N-1:0] = 'b0;   // 不向上游反压
assign dut_active      = 1'b1;  // 始终就绪
assign clkout          = clk1;  // 观察时钟
assign clk             = clk1;  // gpio 用 clk1 作工作时钟
```

再看它如何把 emesh 通道直接转交给 `gpio`（`gpio` 本身就是按 emesh 接口设计的 IP）[gpio/dv/dut_gpio.v:67-83](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L67-L83)：

```verilog
gpio #(.N(AW), .AW(AW)) gpio (
    .gpio_in (gpio_out[AW-1:0]),
    /*AUTOINST*/
    .wait_out   (wait_out),
    .access_out (access_out),
    .packet_out (packet_out[PW-1:0]),
    .gpio_out   (gpio_out[AW-1:0]),
    .gpio_dir   (gpio_dir[AW-1:0]),
    .gpio_irq   (gpio_irq),
    .nreset     (nreset),
    .clk        (clk),
    .access_in  (access_in),
    .packet_in  (packet_in[PW-1:0]),
    .wait_in    (wait_in));
```

两个要点：

- `access_in/wait_out/access_out/packet_in/packet_out/nreset/clk` 直接**透传**给 `gpio`——因为 `gpio` 本来就讲 emesh 协议（见 u5-l1）。所以这个包装几乎「不做适配」，只负责把 `dut` 契约里的通道接到 `gpio` 的同名端口上。
- `gpio` 的引脚相关输出 `gpio_out/gpio_dir/gpio_irq` 是 IP 私有信号，测试平台不关心，于是接到内部 wire 上挂着即可（`gpio_in` 这个输入则被巧妙地回连到 `gpio_out`，形成自环，便于在没有真实 IO 的仿真里观察）。

> **命名碰撞警示**：`dut` 契约里的参数 `N`（=通道数，默认 1）和 `gpio` 模块自己的参数 `N`（=GPIO 引脚数，默认 24）是**两个完全不同的东西**，只是同名。`dut_gpio.v` 用 `gpio #(.N(AW) ...)` 把引脚数显式设成 32（`AW=32`），而 `dut` 的 `N` 由 `dv_top` 传成 1。读这份文件时务必分清「哪个 N 是哪边的 N」。

#### 4.1.4 代码实践

**实践目标**：在不开仿真器的前提下，靠阅读源码把「契约」和「IP 私有信号」分开。

**操作步骤**：

1. 打开 [gpio/dv/dut_gpio.v:1-40](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L1-L40)，把 10 个端口信号分成两组：来自 `dv_top` 契约的固定信号 vs. 你觉得是 IP 私有的信号。
2. 对照 [gpio/hdl/gpio.v:14-27](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L14-L27)（`gpio` 模块自己的端口列表），确认 `gpio_in/gpio_out/gpio_dir/gpio_irq` 确实是 IP 私有、不属于契约。
3. 解释为什么 `dut_gpio.v` 要写 `assign clk = clk1;`，而 `clk` 又不在 `dut` 的端口表里——它必须是什么类型的内部线网？

**需要观察的现象**：你会发现契约端口里没有任何一个 GPIO 引脚信号（`gpio_out` 等）；它们只存在于 `dut` 内部。这正是「外壳不变、内核可换」的体现。

**预期结果**：契约固定信号 = `clk1/clk2/nreset/vdd/vss/access_in/packet_in/wait_in`（入）和 `dut_active/clkout/wait_out/access_out/packet_out`（出）；IP 私有信号 = `gpio_in/gpio_out/gpio_dir/gpio_irq`。`clk` 必须是 `wire`（内部派生线网），因为它既不是端口、又要被 `assign` 驱动。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `dut_active` 接成 `1'b0`（而非 `1'b1`），`dv_top` 侧会发生什么？

**参考答案**：`dut_active` 是「DUT 已就绪/复位完成」的指示，被 `dv_ctrl`/`oh_simctrl` 用作生命周期判断（见 u4-l1/u4-l2）。常 0 会让平台认为 DUT 永远没启动，仿真可能无法正常推进到 `dut_done` 判结论，或只能靠超时（`TIMEOUT`）兜底结束。所以除特殊测试目的外都接 `1'b1`。

**练习 2**：`packet_in` 端口宽度是 `[N*PW-1:0]`，但 `dut_gpio.v` 第 74 行只把 `packet_in[PW-1:0]`（低 104 位）喂给 `gpio`。为什么这样切片是安全的？

**参考答案**：`dv_top` 里 `N=1`，所以 `N*PW-1 = PW-1 = 103`，`packet_in` 实际只有 104 位，`[PW-1:0]` 就是全部。即便将来 `N>1`（多通道），`gpio` 这类单通道 IP 也只该消费第 0 号通道（低 104 位），高地址通道留给别的 IP，所以切片 `[PW-1:0]` 在语义上总是「取第 0 通道」。

---

### 4.2 AUTOINST 与 AUTO_TEMPLATE：verilog-mode 帮你接线

#### 4.2.1 概念说明

在 4.1 的 `gpio` 实例化里你一定注意到了三处奇怪的注释：`/*AUTOINST*/`、`/*gpio AUTO_TEMPLATE (...) */`、`/*AUTOWIRE*/`。它们都来自 **verilog-mode**——一个 Emacs 的 Verilog 插件，OH! 全库用它自动生成端口连接。

核心理解（**最容易误解的一点**）：

> **iverilog 编译器根本不认识这些 `AUTO*` 标记。** 它们只是 Verilog 注释。仓库里提交的 `.v` 文件已经是「verilog-mode 展开后的最终结果」——`/*AUTOINST*/` 标记下面那些 `.port(signal)` 连接，是开发者当初在 Emacs 里运行 verilog-mode 时由它**自动写进去**的文本。iverilog 编译的就是这些已写好的连接，注释标记只起到「这一段是自动生成的，别手改、可重生成」的提示作用。

三个宏各自的职责：

| 标记 | 作用 | 由谁展开 |
|------|------|----------|
| `/*AUTOINST*/` | 按端口**同名**自动生成实例的端口连接（`.port(port)`） | verilog-mode（已展开在文件里） |
| `AUTO_TEMPLATE` | 给 `/*AUTOINST*/` 提供**正则重命名规则**，让端口名按模式连到不同的线网 | verilog-mode（已展开） |
| `/*AUTOWIRE*/` | 为 `/*AUTOINST*/` 产生但尚未声明的输出线网**自动补 `wire` 声明** | verilog-mode（已展开） |

#### 4.2.2 核心流程

verilog-mode 的工作流（开发者侧，不是你跑仿真时做的）：

1. 写一个 `模块名 实例名 ( /*AUTOINST*/ );`，端口先空着。
2. 可选地写一段 `/*模块名 AUTO_TEMPLATE ( 规则 ); */`，用正则规定某些端口要连到哪根线。
3. 在 Emacs 里执行 verilog-mode 的展开命令。
4. verilog-mode 读到被实例模块（如 `gpio.v`）的端口列表，按规则把 `.port(signal)` 一行行填进 `/*AUTOINST*/` 下方，同时在 `/*AUTOWIRE*/` 处补出未声明的 `wire`。

你读源码时，**直接看展开后的结果即可**，那些 `// Templated`、`// From gpio of gpio.v` 注释就是 verilog-mode 留下的「这是自动连的」痕迹。

#### 4.2.3 源码精读

看 `dut_gpio.v` 的 `AUTO_TEMPLATE` 规则 [gpio/dv/dut_gpio.v:62-66](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L62-L66)：

```verilog
/*gpio AUTO_TEMPLATE (
     .gpio_irq     (gpio_irq),
     .gpio_\(.*\)  (gpio_\1[AW-1:0]),
 );
 */
```

这条规则对 `gpio` 实例的端口做两件事：

- 端口 `gpio_irq` → 连到线网 `gpio_irq`（一对一）。
- 任何匹配 `gpio_X` 的端口 → 连到线网 `gpio_X[AW-1:0]`。这里 `\(.*\)` 是捕获组、`\1` 是反向引用，`[AW-1:0]` 给线网加位宽切片。所以 `gpio_out` → `gpio_out[AW-1:0]`、`gpio_dir` → `gpio_dir[AW-1:0]`。

这些自动连出来的线网，由 `/*AUTOWIRE*/` 负责声明，见 [gpio/dv/dut_gpio.v:42-48](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L42-L48)：

```verilog
/*AUTOWIRE*/
wire [AW-1:0] gpio_dir;   // From gpio of gpio.v
wire        gpio_irq;   // From gpio of gpio.v
wire [AW-1:0] gpio_out;   // From gpio of gpio.v
```

注意三个细节：

- `gpio_out/gpio_dir` 被声明成 `[AW-1:0]`，正好对应模板里加的切片位宽。
- 注释 `// From gpio of gpio.v` 是 verilog-mode 自动标注的「这根线来自哪个实例的哪个模块」，方便溯源。
- `gpio_in` 是输入，不在这里声明——它在第 51 行被手写声明为 `wire [AW-1:0] gpio_in;`，因为 AUTOWIRE 只补输出线网。

展开后的实例化结果就是 4.1.3 里看到的 [gpio/dv/dut_gpio.v:67-83](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L67-L83)，每行带 `// Templated` 的就是模板规则产出的连接。

> **如果你不用 Emacs**：完全可以**手写**这些 `.port(signal)` 连接，跳过 `AUTO*` 标记。`AUTO*` 只是省事工具，不是仿真必需。仓库文件已经展开好，iverilog 直接编就行。

#### 4.2.4 代码实践

**实践目标**：验证你真的理解了「模板规则 → 线网名」的映射。

**操作步骤**：

1. 读 [gpio/dv/dut_gpio.v:62-66](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L62-L66) 的正则规则 `.gpio_\(.*\) (gpio_\1[AW-1:0])`。
2. 假设 `gpio` 模块还有一个输出端口叫 `gpio_config`，按规则它会被连到什么线网？
3. 对照 [gpio/dv/dut_gpio.v:42-48](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L42-L48) 的 `/*AUTOWIRE*/` 段，确认 `gpio_dir` 是怎么从规则推导出来的。

**需要观察的现象**：你会看到规则里的 `\1` 就是「`gpio_` 后面那段名字」原样回填，再附上 `[AW-1:0]` 位宽。

**预期结果**：`gpio_config`（若存在）会被连到 `gpio_config[AW-1:0]`，并在 `/*AUTOWIRE*/` 段自动声明 `wire [AW-1:0] gpio_config;`。`gpio_dir` 来自规则对端口 `gpio_dir` 的匹配 → `\1="dir"` → 线网 `gpio_dir[AW-1:0]`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gpio_irq` 要在 `AUTO_TEMPLATE` 里单独写一条，而不是交给通用规则 `.gpio_\(.*\)`？

**参考答案**：通用规则会给线网加 `[AW-1:0]` 位宽切片，但 `gpio_irq` 是单比特中断输出，加 `[AW-1:0]` 会造成位宽不匹配。所以单独写一条 `.gpio_irq (gpio_irq)`（无切片）覆盖通用规则，声明成 1 位 `wire`。

**练习 2**：如果你删掉 `/*AUTOWIRE*/` 这一行（也不手补 wire 声明），iverilog 编译时会报什么错？

**参考答案**：`gpio_dir/gpio_irq/gpio_out` 在实例化处被引用却未声明，iverilog 会把它们当成隐式线网（implicit net）——多数情况下 `-g2005` 默认允许隐式线网而不报错，但位宽会退化成 1 位，导致 `gpio_out[AW-1:0]` 的多位连接出错或截断。这正说明 `/*AUTOWIRE*/` 不是装饰，它声明的位宽是真有用的。（严格工程实践里会开 `-Wall` 并禁用隐式线网来抓这类问题。）

---

### 4.3 端到端仿真：从 .emf 一行事务到 DUT 响应

#### 4.3.1 概念说明

前两节讲的是「静态结构」——`dut` 包装怎么写。本节把时间维度接上，看一个事务从 `.emf` 文本文件出发，经过 `stimulus` 回放、`dut` 包装、被测 IP，最后回到 `dv_driver` 的监视器，**完整跑一圈**。这就是「端到端仿真」。

回顾三步流程（u1-l3 已搭好环境）：

1. **build**：`build.sh dut_gpio.v` → 产出 `dut.bin`。
2. **sim**：`sim.sh tests/test_basic.emf` → 把 `.emf` 软链成 `test_0.emf`，运行 `dut.bin`，产出 `waveform.vcd`。
3. **view**：`view.sh` → gtkwave 打开波形。

#### 4.3.2 核心流程

一个 gpio 写事务在时间轴上的旅行：

```
test_basic.emf 第1行 ──$readmemh──▶ stimulus 的 ram[]
                                   │
                          (按 dut_ready 节拍回放)
                                   ▼
              stim_access / stim_packet (104位包)  ← dv_driver 输出
                                   │
                                   ▼ (dv_top 连线)
              dut.access_in / dut.packet_in        ← dut 包装输入
                                   │
                                   ▼ (dut_gpio 透传)
              gpio.access_in / gpio.packet_in
                                   │
                                   ▼ (gpio 内部 enoc_unpack 拆字段+地址译码)
              reg_write=1, data=FFFF0000 → 写入 GPIO_DIR 寄存器
                                   │
              (若是读事务) gpio 把寄存器值塞进 packet_out
                                   ▼
              dut.packet_out / dut.access_out
                                   │
                                   ▼
              dv_driver 的 emesh_monitor / ememory 收响应
                                   │
                                   ▼
              所有通道 done → oh_simctrl 判 PASSED/FAILED，dump VCD
```

#### 4.3.3 源码精读

先看编译命令 [scripts/build.sh:15-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh#L15-L19)：

```bash
iverilog -g2005 \
 -DTARGET_SIM=1 \
 -DCFG_ASIC=0 \
 -f $OH_HOME/scripts/libs.cmd \
 -o dut.bin $1
```

含义：`-g2005` 锁定 Verilog 2005；`-DTARGET_SIM=1` 打开仿真专用分支；`-DCFG_ASIC=0` 选 soft（RTL）实现；`-f libs.cmd` 读入库搜索路径；`$1` 是你的 `dut_xxx.v`。**注意 `dv_top` 及整套测试平台文件不在命令行里显式列出——它们靠 `libs.cmd` 的 `-y` 路径按文件名自动找到**（iverilog 的 `-y` 机制：遇到未定义模块名就去 `-y` 目录找 `模块名.v`）。

再看 `sim.sh`（[scripts/sim.sh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/sim.sh) 全文）：

```bash
if [ -L "test_0.emf" ]; then unlink test_0.emf; fi
ln -s $1 test_0.emf   # 把传入的 .emf 软链成 dv_driver 期望的固定文件名
./dut.bin             # 运行仿真，dump waveform.vcd
```

激励文件 `test_basic.emf`（[gpio/dv/tests/test_basic.emf:1-11](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/tests/test_basic.emf#L1-L11)）每行一个事务：

```
DEADBEEF_FFFF0000_00000000_05_0010 // write gpio_dir
DEADBEEF_FFFF0000_00000010_05_0010 // write gpio_out
...
DEADBEEF_DEADBEEF_00000008_04_0010 // read gpio_in
```

字段顺序由 `egen.pl` 的 `printf` 权威定义（[emesh/dv/egen.pl:161-162](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L161-L162)）：`%08x_%08x_%08x_%02x_0000` 对应 `datahi_datalo_dstaddr_ctrlmode_access`。以第 1 行为例：

| 字段 | 值 | 含义 |
|------|----|----|
| datahi（兼作 srcaddr/返回地址） | `DEADBEEF` | 读响应回送地址（写事务里不关键） |
| datalo | `FFFF0000` | 真正写入的 32 位数据（全部引脚设为输出方向） |
| dstaddr | `00000000` | 目标地址 = GPIO_DIR（`addr[6:3]=0`，见 [gpio/hdl/gpio_regmap.vh:4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L4)） |
| ctrlmode | `05` = `0b0101` | bit0=1 写；bits[2:1]=2 即字（32 位） |
| access | `0010` | 事务有效/控制位（详见 u4-l2） |

把这行对照 [gpio/hdl/gpio_regmap.vh:4-14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L4-L14) 的地址表，能看出 11 行事务正好覆盖了 `gpio_dir/out/outclr/outset/outxor/imask/itype/ipol` 的写，以及对 `gpio_in/ilat` 的读——这就是一份最小回归测试。

> **现实提示（重要）**：当前仓库的这套脚本**不能开箱即跑**，原因有三，读源码时务必先看清：
>
> 1. `build.sh` 的 `-f $OH_HOME/scripts/libs.cmd` 指向的 `scripts/libs.cmd` **不存在**；真正的清单在 [stdlib/testbench/libs.cmd](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/libs.cmd)。
> 2. 那份 `libs.cmd` 里既没有 `-y ../../stdlib/rtl`、也没有 `-y ../../stdlib/testbench`——也就是说 `oh_counter`、`dv_top`、`stimulus` 这些**都不在搜索路径上**；同时还引用了已不存在的 `common/`、`memory/`、`accelerator/` 目录（见 u1-l2、u4-l1）。
> 3. `build_all.sh` 用的是 `$OH_HOME/src/$dut/...`，而 `src/` 目录不存在。
>
> 所以「跑通 build+sim」前，你需要先补路径（见综合实践的步骤 3）。这是 OH! 仓库的历史遗留，不是你的操作有误。

#### 4.3.4 代码实践

**实践目标**：不跑仿真，纯靠阅读把一个 `.emf` 事务「翻译」成对寄存器的具体影响。

**操作步骤**：

1. 取 `test_basic.emf` 第 3 行 `DEADBEEF_33330000_00000018_05_0010 // write gpio_outclr`。
2. 拆出 `dstaddr=0x18`，查 [gpio/hdl/gpio_regmap.vh:4-14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L4-L14)：`0x18 >> 3 = 0x3 = GPIO_OUTCLR`。
3. 取 `datalo=0x33330000`，结合 `ctrlmode=05`（写、字），说明这一行把 `GPIO_OUT` 的哪些位清零。
4. 追踪它进入 `dut` 后的路径：`packet_in[PW-1:0]` → `gpio.packet_in` → `gpio` 内部拆包 → `outclr_write` 选通。

**需要观察的现象**：地址低 3 位（`addr[2:0]`）在译码里被忽略，因为寄存器按 `addr[6:3]` 索引（每个寄存器占 8 字节对齐空间）。

**预期结果**：第 3 行把 `GPIO_OUT` 中对应 `0x33330000` 为 1 的那些位清零（`OUTCLR` 的语义是「写 1 清零」）。这与 `test_basic.emf` 第 2 行先写 `0xFFFFFFFF`（全置 1）配合，可观察 `gpio_out` 从全 1 变成 `0xCCCCFFFF`。

#### 4.3.5 小练习与答案

**练习 1**：第 9 行 `DEADBEEF_DEADBEEF_00000008_04_0010 // read gpio_in` 的 `ctrlmode=04`。相对写事务，它少了什么？`datahi/datalo` 在读事务里还有意义吗？

**参考答案**：`04 = 0b0100`，bit0=0 表示**读**。读事务里 `datalo` 无意义（不写数据），`datahi` 兼作的 `srcaddr` 才有意义——它是读响应要回送的目标地址，`gpio` 完成读后会用这个地址组装返回包。

**练习 2**：为什么 `build.sh` 命令行里只列了 `dut_gpio.v` 一个文件，却没有 `dv_top.v`、`gpio.v`、`stimulus.v`？它们是怎么被纳入编译的？

**参考答案**：iverilog 从顶层 `dut_gpio.v`（注意：实际编译入口还涉及 `dv_top`，但 iverilog 会从模块实例化关系递归查找）开始，遇到任何未定义的模块名（`dv_top`、`gpio`、`stimulus`、`oh_counter`…），就按 `libs.cmd` 里 `-y` 给出的目录、以「模块名.v」为文件名去搜索并自动纳入。这就是 `-y` 库搜索机制（见 u1-l3）。

---

## 5. 综合实践：为 oh_counter 写一个 dut 包装

把本讲三个模块（`dut` 契约、`AUTOINST`、端到端流程）串起来，完成 spec 要求的任务：**仿照 `dut_gpio.v`，为 `oh_counter` 写一个 `dut` 包装，并尝试跑通 build+sim。**

### 5.1 实践目标

- 写出 `dut_counter.v`：遵守 `dut` 固定端口契约，内部实例化 [oh_counter](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v)。
- 让计数器「可观察」：把 `.emf` 注入的数据装载进计数器，再把计数器当前值回送进 `packet_out`，能在波形里看到计数与装载。
- 亲手处理仓库脚本遗留的路径问题，理解 `-y` 搜索机制。

### 5.2 操作步骤

**步骤 1——读懂目标原语的端口**。先看 [stdlib/rtl/oh_counter.v:13-25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v#L13-L25)：

```verilog
input          clk, in, en, dec, autowrap, load;
input  [N-1:0] load_data;
output reg [N-1:0] count;
output         wraparound;
```

注意两个特点：`oh_counter` **没有复位端口**（`count` 上电后是 X，靠 `load` 装载初值）；它的核心逻辑在 [stdlib/rtl/oh_counter.v:35-39](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v#L35-L39)，`load` 优先于计数。

**步骤 2——写 `dut_counter.v`**。下面是**示例代码**（仓库里不存在，需你自行创建）：

```verilog
// 示例代码：oh_counter 的 dut 包装，仿照 dut_gpio.v / dut_gray.v
module dut(/*AUTOARG*/
   // Outputs
   dut_active, clkout, wait_out, access_out, packet_out,
   // Inputs
   clk1, clk2, nreset, vdd, vss, access_in, packet_in, wait_in
   );

   parameter N  = 1;     // 通道数（dv_top 会传 1）
   parameter PW = 104;   // 包宽
   parameter CW = 32;    // 计数器位宽（自定义，与 dut 的 N 区分）

   input            clk1, clk2, nreset, vss;
   input [N*N-1:0]  vdd;
   output           dut_active, clkout;
   input  [N-1:0]     access_in;
   input  [N*PW-1:0]  packet_in;
   output [N-1:0]     wait_out;
   output [N-1:0]     access_out;
   output [N*PW-1:0]  packet_out;
   input  [N-1:0]     wait_in;

   /*AUTOWIRE*/
   wire [CW-1:0] count;

   // 兜底
   assign dut_active = 1'b1;
   assign wait_out   = 'b0;
   assign clkout     = clk1;
   assign access_out = access_in;        // 环回，让 monitor 看到响应节拍
   wire clk = clk1;

   // 把 emesh 包的数据字段 packet_in[39:8]（32 位）当作装载值；
   // 每个 access_in 脉冲（stimulus 注入事务）触发一次装载；
   // 其余时钟周期计数器自由递增。
   wire [CW-1:0] load_data = packet_in[39:8];
   wire          load      = access_in[0];

   oh_counter #(.N(CW)) u_counter (
      .clk        (clk),
      .in         (1'b1),       // 步长 1
      .en         (1'b1),       // 每拍都计数（load 期间硬件自动优先装载）
      .dec        (1'b0),       // 递增
      .autowrap   (1'b1),       // 到边界回绕
      .load       (load),
      .load_data  (load_data),
      .count      (count[CW-1:0]),
      .wraparound ());           // 不观察

   // 把当前计数值塞回包的数据字段，便于在波形/响应里观察
   assign packet_out[39:8] = count[CW-1:0];

endmodule // dut
```

设计要点（与 4.1 的套路一一对应）：

- **契约照抄**：端口与 `dut_template.v` 完全一致。
- **兜底**：`dut_active/wait_out/clkout/access_out` 各就各位；`access_out=access_in` 模仿 [stdlib/testbench/dut_gray.v:37-40](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_gray.v#L37-L40) 的环回写法。
- **字段映射**：`packet_in[39:8]` 取 32 位数据（与 `dut_gray.v` 用同一个字段位置，见 [stdlib/testbench/dut_gray.v:43-50](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_gray.v#L43-L50)）；`access_in[0]` 当装载脉冲。
- **回送观察**：`count` 写回 `packet_out[39:8]`，这样在波形里既能看到注入的装载值，也能看到每拍递增的计数。

**步骤 3——修脚本路径**（绕开 4.3.3 列出的遗留问题）。最省事的做法是不依赖 `build.sh`，直接手敲一条修正过的 iverilog 命令（待本地验证：取决于你机器上 iverilog 版本与具体报错）：

```bash
# 假设你在仓库根目录，OH_HOME 指向它
iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0 \
  -y stdlib/rtl -y stdlib/testbench -y emesh/hdl -y gpio/hdl \
  -o dut.bin stdlib/testbench/dv_top.v stdlib/testbench/dut_counter.v
```

关键修正：手动补上 `-y stdlib/rtl`（找 `oh_counter.v`）和 `-y stdlib/testbench`（找 `dv_top.v`/`stimulus.v`/`oh_simctrl.v` 等），并**显式列出 `dv_top.v` 与你的 `dut_counter.v` 作为编译入口**，绕开不存在的 `scripts/libs.cmd`。

**步骤 4——准备最小激励**。仿照 `test_basic.emf` 写一个 `test_counter.emf`（示例代码）：

```
00000000_00000010_00000000_05_0010 // 装载 count=0x10（access_in 触发 load）
00000000_00000020_00000000_05_0010 // 装载 count=0x20
00000000_00000000_00000000_04_0010 // 读（观察 packet_out 里的 count）
```

**步骤 5——运行并看波形**：

```bash
ln -sf test_counter.emf test_0.emf
./dut.bin
# 用 gtkwave 打开 waveform.vcd，观察 count / access_in / packet_out[39:8]
```

### 5.3 需要观察的现象

- 复位释放后、第一个 `access_in` 脉冲到来前，`count` 是 `x`（因为 `oh_counter` 无复位）。
- 每次 `access_in` 拉高的那个时钟沿，`count` 跳变为 `packet_in[39:8]` 的值（装载）。
- `access_in` 为低的时钟周期里，`count` 每拍 +1（自由递增）。
- `packet_out[39:8]` 实时跟随 `count`。

### 5.4 预期结果

若路径修正正确、编译通过，波形应呈现「装载→递增→再装载→递增」的阶梯。**若编译报「找不到模块」**，大概率是某个 `-y` 路径漏了（如 `emesh/hdl` 找不到 `enoc_unpack` 之类），按报错补路径即可。

> **待本地验证**：由于当前仓库的测试平台本身存在接口漂移（`dv_driver` 实例化的 `stimulus`/`ememory` 与现版 `stimulus.v` 及缺失的 `ememory.v` 对不上，见 u4-l2），`dv_top` 这条路未必能一次性编通。如果你只想验证「`dut_counter.v` 本身的写法对不对」，可以用 stdlib 自己的**简化测试平台**（`sim.v` + `tb_*.v`）作替代——[stdlib/testbench/run.sh:5-6](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/run.sh#L5-L6) 展示了这套更简单的用法：`iverilog ... sim.v tb_oh_lfsr.v -y ../rtl/ -y .`，它绕开了 `dv_top` 与 `.emf`，直接用 `tb_*.v` 给原语注入激励。这是 stdlib 测试自己原语时实际采用的模式。

## 6. 本讲小结

- **`dut` 包装 = 固定外壳 + 可换内核**：端口契约由 [dv_top.v:67-84](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L67-L84) 写死（5 出 7 入 + `N/PW` 参数），换 IP 只需写新 `dut_xxx.v`、在编译期替换。
- **写法四步**：抄契约 → 兜底（`dut_active/wait_out/clkout`）→ 派生时钟 → 实例化 IP 并桥接事务字段；`dut_gpio.v` 是「IP 本身讲 emesh」的最直接范例。
- **`AUTO*` 是 verilog-mode 的产物，与 iverilog 无关**：`/*AUTOINST*/` 同名连接、`AUTO_TEMPLATE` 正则重命名、`/*AUTOWIRE*/` 补线网声明；仓库文件已展开，直接读展开结果即可，不用 Emacs 也能手写。
- **端到端 = build → sim → view**：`.emf` 一行 = `datahi_datalo_dstaddr_ctrlmode_access`（[egen.pl:161](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L161)），经 `stimulus` 回放进 `dut`，IP 处理后响应回到 monitor。
- **仓库脚本有遗留**：`scripts/libs.cmd` 不存在、`libs.cmd` 未含 stdlib 路径且引用了已删除的 `common/memory/accelerator`、`build_all.sh` 用了不存在的 `src/`；端到端跑通前需先补 `-y` 路径或改用 `sim.v + tb_*.v` 简化平台。
- **`oh_counter` 无复位端口**：包装时靠 `load` 装初值，复位后到首次装载前输出为 `x`，这是要在波形里预见的现象。

## 7. 下一步学习建议

- **进入第 5 单元 emesh 协议**：本讲反复出现的 `packet_in[39:8]`、`ctrlmode`、104 位包只是字段位置；u5-l1 会把 104 位（`PW=2*AW+40`）的完整位序（`dstaddr/srcaddr/data/datamode/write/access/ctrlmode`）讲透，理解之后你就能精确知道每个字段在第几位、包装时该切哪段。
- **读更多 `dut_*.v` 巩固套路**：[dut_gray.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_gray.v)（环回）、[dut_clockdiv.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_clockdiv.v)（取控制位）、[dut_fifo_generic.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_fifo_generic.v)（直连 access/packet/wait），三种典型填法对照看。
- **若你要正式贡献一个新 IP**：参考 u9-l5 的流片检查清单与编码规范，把 `dut` 包装 + `regmap.vh` + `test_basic.emf` 三件套配齐，按 OH! 约定接入仿真。
