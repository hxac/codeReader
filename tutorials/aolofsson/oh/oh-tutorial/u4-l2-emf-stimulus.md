# u4-l2 激励驱动与 .emf 测试格式

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂一行 `.emf` 测试文件的五个字段，并能手动拆解出一个读/写事务。
- 说清楚 `dv_driver` 这一层「激励回放 + 事务监视 + 仿真存储」三件套各自的角色。
- 描述 `oh_simctrl` 如何用一个状态序列驱动整个仿真的生命周期（复位 → 加载 → 启动 → 结束/超时）。
- 用 `egen.pl` 生成一组随机 `.emf` 事务，并解释它输出的「写 / 读 / 期望值」三类行。

本讲承接 u4-l1 建立的 `dv_top` 三段式骨架（`dv_ctrl` + `dut` + `dv_driver`），把镜头推进到「激励从哪里来、怎么一格一格灌进 DUT、仿真又由谁来叫停」。本讲只讲测试平台运行时，不展开 emesh 包内部的 104 位精确位布局（那是 u5-l1 的任务）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**(1) 事务（transaction）而非波形。** 给数字电路灌测试激励有两种极端做法：一是手写波形（什么时候拉高哪根线），二是写「一串高层事务」（向地址 A 写数据 D、再从地址 A 读回）。前者贴着管脚、难以维护；后者贴近协议、可读性强。OH! 选择后者，并把每个事务编码成一行人类可读的十六进制文本，这就是 `.emf` 文件。

**(2) 回放（replay）。** 把一串事务存进一块片上存储（本质是 testbench 里的 `reg [...] ram[0:DEPTH-1]`），再用一个小状态机一格一格读出来、按握手协议送给 DUT。这套「先把激励装进 RAM，再依节拍吐出来」的机制就叫回放器（stimulus player）。

**(3) 仿真生命周期。** 一个 testbench 必须回答四个问题：什么时候复位、时钟怎么翻、怎么知道测完了、跑飞了怎么办。这四件事由一个「仿真控制器」集中管理，OH! 里叫 `oh_simctrl`。u4-l1 已经把它概括成「复位时序 + 时钟翻转 + 完成判结论 + 超时兜底」四件套范式，本讲会把它落到源码上。

涉及的关键术语：`.emf`（OH! 的事务激励文本格式）、`$readmemh`（Verilog 系统任务，把十六进制文本读进存储数组）、access/packet/wait 握手（u4-l1 已建立）、emesh 包（104 位片上网络事务包，详见 u5-l1）、mode 状态（idle/load/go/rng/bypass）。

> ⚠️ 延续 u4-l1 的结论：本仓库的测试平台存在历史演进留下的「接口漂移」。`dv_driver.v` 里实例化的 `stimulus` 与当前仓库里的 `stdlib/testbench/stimulus.v` 端口/参数并不一致，`ememory` 模块在仓库中找不到对应文件，stim/mem 多路选择还是 `//TODO`。因此本讲在讲 `dv_driver` 的「设计意图」时以 `dv_driver.v` 为准，在讲「真实可编译的回放状态机」时以 `stimulus.v` 为准——遇到不一致，一律以源码实际文本为事实。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [stdlib/testbench/dv_driver.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_driver.v) | 测试平台运行时：实例化回放器 `stimulus`、监视器 `emesh_monitor`、仿真存储 `ememory`，按 N 条通道 `generate` 展开。 |
| [stdlib/testbench/stimulus.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/stimulus.v) | 真正用 `$readmemh` 加载 `.emf`、用状态机回放激励的核心模块（IDLE→ACTIVE→PAUSE→DONE）。 |
| [stdlib/testbench/oh_simctrl.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v) | 仿真控制器：产时钟、拉复位、走 mode 序列、判 PASSED/FAILED、超时兜底。 |
| [elink/dv/tests/test_hello.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf) | 一个真实可读的 `.emf` 样例：先写 16 个字到 GPIO 区，再逐个读回。 |
| [emesh/dv/egen.pl](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl) | 随机事务生成器：按字节预算生成随机写、去重、再追加读与期望值。 |
| [scripts/sim.sh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/sim.sh) | 运行脚本：把指定 `.emf` 软链成 `test_0.emf`，再执行编译好的 `dut.bin`。 |

## 4. 核心概念与源码讲解

### 4.1 .emf 测试文件格式

#### 4.1.1 概念说明

`.emf` 是 OH! 对「事务激励文本」的命名（emesh file）。它的设计目标只有一个：**用一行人类可读的十六进制表示一个对 DUT 的事务**，让测试既可以手写、也可以用脚本（`egen.pl`）批量生成。

一行 `.emf` 由 5 个用下划线 `_` 分隔的十六进制段组成，自左向右是：

```
<srcaddr/datahi>_<datalo>_<dstaddr>_<ctrlmode>_<access>
   8 hex(32b)    8 hex(32b)  8 hex(32b)  2 hex(8b)  4 hex(16b)
```

这里的关键认知是：**前四段承载 emesh 事务的字段（数据 + 目的地址 + 控制），第五段是回放器自己用的 access/控制位**，并不属于 emesh 104 位包本身。下划线只是给人看的分组符号，Verilog 的 `$readmemh` 会把整行当成一个十六进制数读入（下划线在十六进制字面量里是合法分隔符）。

需要特别说明字段顺序的一个细节：`.emf` 把「目的地址」放在第三段、把「控制」放在第四段，是为了让地址和数据对齐美观；它**不等于** emesh 包在 104 位总线上的物理位序。位序的精确定义留给 u5-l1，本讲只关心「一行事务的含义」。

#### 4.1.2 核心流程

把 `.emf` 喂给仿真，链路是这样的：

1. **打包**：测试作者（或 `egen.pl`）把每个事务编码成一行五段十六进制。
2. **加载**：仿真启动时，回放器用 `$readmemh("test_0.emf", ram)` 把整份文件一次性读进内部 `ram` 数组，一行对应一个存储字。
3. **回放**：一个小状态机按 DUT 的 ready 节拍，逐字读出 `ram`，把前四段当成事务内容、第五段当成有效/结束标记，送进 DUT 的 access/packet 接口。
4. **对照**：读事务的返回数据会被监视器抓进一个 trace 文件，与 `.emf` 末尾的「期望值」行比对（见 4.3）。

最可信的字段定义证据是 `egen.pl` 构造每一行时用的 `printf` 格式串——它直接说明了每一段装的是什么。

#### 4.1.3 源码精读

先看一段真实写事务，来自 elink 的 `test_hello.emf`：

[elink/dv/tests/test_hello.emf:1-4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf#L1-L4)

```
DEADBEEF_00001001_810f0210_05_0000 //CONFIG-LOOPBACK
00000000_00000000_80800000_05_0010 //WRITE
11111111_11111111_80800004_05_0010
22222222_22222222_80800008_05_0010
```

第 1 行是一次「配置写」（往 elink 配置寄存器 `0x810f0210` 写 `0x00001001`，loopback 模式）；第 2~ 行是连续向 GPIO 区 `0x80800000` 起的字地址写数据。

再看同文件里的读回段：

[elink/dv/tests/test_hello.emf:18-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf#L18-L19)

```
810d0000_DEADBEEF_80800000_04_0000 // read
810d0004_DEADBEEF_80800004_04_0000
```

第一段从写的「数据高位」变成了**返回地址（srcaddr）** `0x810d0000`，第二段填占位符 `0xDEADBEEF`（读请求里数据无意义），第四段的控制码从 `05` 变成 `04`（把「写」位清掉）。这正对应 u4-l1 提过的「同一组字段、靠控制位区分读写」。

字段含义的唯一权威来源是 `egen.pl` 生成写事务时的格式串：

[emesh/dv/egen.pl:161-162](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L161-L162)

```perl
printf("%08x_%08x_%08x_%02x_0000//WRITE (i=%d, j=%d, bytes=%d)\n",
       $datahi,$datalo,$dstaddr,$ctrlmode, $count, $j, $bytes);
```

对照可得各段语义：第一段 = `datahi`（写时的数据高位，双字才用；读时复用为 srcaddr），第二段 = `datalo`（数据低位），第三段 = `dstaddr`（目的地址），第四段 = `ctrlmode`（8 位控制），第五段 = `0000`（egen 固定输出 0，`test_hello.emf` 里手写成 `0010/0040`）。

第四段 `ctrlmode` 的 8 位是 `.emf` 里信息密度最高的字段。把 `egen.pl` 里按尺寸分流的代码和它的写位约定（写事务 `| 0x1`）合起来，可以还原出下表：

[emesh/dv/egen.pl:101-128](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L101-L128) 给出了尺寸到地址增量、掩码的映射；[emesh/dv/egen.pl:93-94](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L93-L94) 强制写事务置写位。

| ctrlmode（hex） | bit0 写位 | bits[2:1] datamode | 含义 | 地址增量 | 有效数据 |
| --- | --- | --- | --- | --- | --- |
| `0x01` / `0x00` | 1 写 / 0 读 | 00 | 字节（8 位） | 1 | datalo 低 8 位 |
| `0x03` / `0x02` | 1 写 / 0 读 | 01 | 半字（16 位） | 2 | datalo 低 16 位 |
| `0x05` / `0x04` | 1 写 / 0 读 | 10 | 字（32 位） | 4 | datalo 全 32 位 |
| `0x07` / `0x06` | 1 写 / 0 读 | 11 | 双字（64 位） | 8 | datahi ++ datalo |

于是 `test_hello.emf` 里的 `05` = 「写一个 32 位字」，`04` = 「读一个 32 位字」，完全自洽。datamode 表示的字节数满足：

\[ \text{字节数} = 8 \ll \text{datamode} \quad (\text{即 } 8,\,16,\,32,\,64) \]

第五段 `access`（4 个十六进制 = 16 位）是回放器侧的控制位，egen.pl 恒输出 `0000`；`test_hello.emf` 中流内写用 `0010`、最末一次写用 `0040`、读用 `0000`，其精确位语义由消费它的具体 testbench 定义，本讲不臆断。

#### 4.1.4 代码实践：手写一个 .emf（本讲主实践）

**实践目标**：不用任何脚本，手写一个最小 `.emf`，对地址 `0x80800000` 连续写 4 个不同的 32 位字，再逐个读回。

**操作步骤**：

1. 新建文件 `my_test.emf`（放在你能运行仿真的目录，或仅作为文本练习）。
2. 模仿 `test_hello.emf` 的五段格式。写事务：第一段（datahi）对 32 位字写无意义，填 `00000000`；第二段（datalo）填你的 32 位数据；第三段填目的地址；第四段填 `05`（写字）；第五段填 `0010`。
3. 读事务：第一段填返回地址 `810d0000`；第二段填占位 `DEADBEEF`；第三段填被读地址；第四段填 `04`（读字）；第五段填 `0000`。
4. 让地址每次 +4（一个 32 位字 = 4 字节）。

参考答案（示例代码，仿照 `test_hello.emf` 风格）：

```
00000000_11112222_80800000_05_0010 //WRITE word 0
00000000_33334444_80800004_05_0010 //WRITE word 1
00000000_55556666_80800008_05_0010 //WRITE word 2
00000000_77778888_8080000c_05_0010 //WRITE word 3
810d0000_DEADBEEF_80800000_04_0000 //READ word 0
810d0000_DEADBEEF_80800004_04_0000 //READ word 1
810d0000_DEADBEEF_80800008_04_0000 //READ word 2
810d0000_DEADBEEF_8080000c_04_0000 //READ word 3
```

**需要观察的现象**：逐行核对——每个写事务的地址都比上一行多 4；读事务的地址序列与写事务一一对应；读请求的第二段都是 `DEADBEEF`（占位），写请求的第二段才是真数据。

**预期结果**：8 行事务，4 写 4 读，地址连续、不重叠、4 字节对齐。

**关于运行**：要让这文件真正跑起来，需要一个把它 `$readmemh` 进去的 testbench（见 4.2）。如果你尚未搭建可编译的环境（u1-l3 指出仓库脚本有遗留路径问题），这一步可先作为「文本格式练习」，把运行验证标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：把 `test_hello.emf` 第 1 行 `DEADBEEF_00001001_810f0210_05_0000` 的每个字段写出来。
**答案**：datahi=`DEADBEEF`、datalo=`00001001`（即写入的配置值）、dstaddr=`810f0210`（elink 配置寄存器地址）、ctrlmode=`05`（写一个 32 位字）、access=`0000`。

**练习 2**：如果要把一个 64 位双字 `0xAABBCCDDEEFF0011` 写到 `0x80800000`，这行该怎么写？
**答案**：双字用 ctrlmode `07`，高 32 位进第一段、低 32 位进第二段：`AABBCCDD_EEFF0011_80800000_07_0010`。

**练习 3**：为什么读请求的第二段通常写 `DEADBEEF`？
**答案**：读事务不携带写入数据，第二段（datalo）对读无意义；填一个显眼的占位符（如 `DEADBEEF`）是为了在 trace 里一眼分辨「这是请求」还是「这是返回数据」，便于人眼排查。

---

### 4.2 dv_driver 与仿真生命周期

#### 4.2.1 概念说明

`dv_driver` 是 `dv_top` 三段式里「驱动 + 监视」那一段的实现（u4-l1 已画过三段连接）。它的内部职责分成三块：

- **回放（stimulus）**：从 `.emf` 加载激励，按 DUT 的 ready 节拍逐事务吐出。
- **监视（emesh_monitor）**：在 `clkout` 域盯着 DUT 吐出的有效事务，把它写进 trace 文件，供事后比对。
- **仿真存储（ememory）**：扮演一个「应答 slave」，给落到它地址范围的读请求回数据。

而这三块的「开关」——什么时候开始吐激励、什么时候算测完——由一个独立的仿真控制器 `oh_simctrl` 掌管。所以本最小模块实际上讲两个文件：`dv_driver.v`（运行时容器）和 `oh_simctrl.v`（生命周期指挥）。

#### 4.2.2 核心流程

**回放流水**（数据从 `.emf` 到 DUT）：

```
.emf 文件 --$readmemh--> stimulus.ram[] --状态机读出--> stim_packet/stim_valid --(access/packet/wait)--> DUT
                                              ^
                                     oh_simctrl: start/mode 拍板启动
```

**仿真生命周期**（`oh_simctrl` 的时间线）：

```
t=0        : nreset=0（拉低复位），mode=idle
+TIME_RESET: nreset=1（释放复位）
+TIME_WAIT : mode=load（加载阶段）
+TIME_LOAD : mode=go（或 rng，取决于 RANDOM_DATA）——回放器开始吐激励
...        : DUT 运行；任一拍 dut_done=1 即收尾
dut_done=1 : 等 500，看 dut_fail：0→PASSED / 1→FAILED，$finish
TIMEOUT    : 仍没 done → 打印 TIMEOUT，$finish（兜底）
```

两条线在「mode=go」处交汇：`oh_simctrl` 把 mode 拉到 go，回放器才真正开始把 `ram` 里的内容送给 DUT。

#### 4.2.3 源码精读

**(a) dv_driver：按通道展开的三件套**

`dv_driver` 用 `generate for` 把回放器铺成 N 条通道，只有前 `STIMS` 条真正接了 `stimulus`，其余通道被常量绑死（access=0、done=1），相当于「占位通道」。所有通道的 done 做 AND 归约，得到全局 `stim_done`：

[stdlib/testbench/dv_driver.v:44-44](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_driver.v#L44-L44) —— `assign stim_done = &(stim_vec_done[N-1:0]);`，与归约表示「N 条通道全部回放完才算完」。

[stdlib/testbench/dv_driver.v:46-76](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_driver.v#L46-L76) 是通道展开的 generate 块；活跃分支实例化 `stimulus`，否则分支把信号绑死：

```verilog
generate
   for(i=0;i<N;i=i+1) begin : stim
      if(i<STIMS) begin
         stimulus #(.PW(PW), .INDEX(i), .NAME(NAME)) stimulus ( ... );
      end
      else begin
         assign stim_access[i]               = 'b0;
         assign stim_packet[(i+1)*PW-1:i*PW] = 'b0;
         assign stim_vec_done[i]             = 'b1;   // 占位通道直接「已完成」
         ...
      end
   end
endgenerate
```

> ⚠️ 注意：`dv_driver.v` 在这里实例化的 `stimulus` 用的是 `.PW/.INDEX/.NAME` 参数和 `.stim_access/.stim_count/.stim_wait/.start/.dut_wait` 端口（[L50-L64](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_driver.v#L50-L64)），而当前仓库的 `stdlib/testbench/stimulus.v` 并没有这些端口（它的端口见 4.2.3(b)）。这是前述「接口漂移」——`dv_driver.v` 期待一个更「emesh 化」的 stimulus，与现版 `stimulus.v` 对不上。读源码时把两者当成分属不同演进阶段的版本即可。

监视器（monitor）在 `clkout` 域工作，把 DUT 吐出的每包事务交给 `emesh_monitor`：

[stdlib/testbench/dv_driver.v:92-111](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_driver.v#L92-L111) —— 同样按通道展开，每条通道挂一个 `emesh_monitor`。

仿真存储 `ememory` 想扮演应答 slave：

[stdlib/testbench/dv_driver.v:116-138](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_driver.v#L116-L138) —— 但仓库中没有 `ememory.v` 文件（Glob 查无），且文件末尾的 stim/mem 多路选择标注为 `//TODO: Implement`（[L140-L145](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_driver.v#L140-L145)）。所以 `dv_driver.v` 当前是一份「架构意图」而非可直接编译的实现。

**`emesh_monitor`** 本身（在 emesh 模块里）的逻辑很短：仅在参数 `ENABLE=1` 时生效，在 `clk` 上升沿且 `dut_valid & ready_in` 时把当前包按字段写进一个 trace 文件：

[emesh/hdl/emesh_monitor.v:38-46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_monitor.v#L38-L46)

```verilog
always @ (posedge clk)
  if(nreset & dut_valid & ready_in)
    if (PW==112) ... $fwrite(ftrace, "%h_%h_%h_%h\n", dut_packet[110:80], ...);
```

这正好解释了 elink README 里 `test_0.trace` 的来历——监视器把 DUT 实际吐出的事务记成文本，再和 `.emf` 里的期望值 `diff` 比对。

**(b) stimulus：真正加载 .emf 的状态机**

把目光从「意图」转回「可编译的现实」。`stdlib/testbench/stimulus.v` 才是用 `$readmemh` 真正加载文件的那个模块。它的关键三段：

加载（仿真开始时一次性读入）：

[stdlib/testbench/stimulus.v:56-63](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/stimulus.v#L56-L63)

```verilog
generate
   if(!(FILENAME=="NONE"))
     initial begin
        $display("Driving stimulus from %s", FILENAME);
        $readmemh(FILENAME, ram);     // 一行一个字，读进 ram[]
     end
endgenerate
```

回放状态机（IDLE→ACTIVE→PAUSE→DONE，受 `dut_ready` 节拍控制）：

[stdlib/testbench/stimulus.v:91-115](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/stimulus.v#L91-L115)

```verilog
case (rd_state[1:0])
  STIM_IDLE  : rd_state <= dut_start ? STIM_ACTIVE : STIM_IDLE;
  STIM_ACTIVE: begin
     rd_state <= (|rd_delay) ? STIM_PAUSE :    // 还在延时？暂停
                 ~stim_valid ? STIM_DONE  :    // 读到无效字？结束
                              STIM_ACTIVE;
     rd_addr  <= rd_addr + 1'b1;
  end
  STIM_PAUSE : begin rd_state <= (|rd_delay)?STIM_PAUSE:STIM_ACTIVE; rd_delay<=rd_delay-1; end
endcase
```

这段状态机把「按 ready 节拍回放」「支持插入延时（PAUSE）」「遇到结束标记即停（DONE）」三件事用十几行讲清楚了。有效与结束的判定：

[stdlib/testbench/stimulus.v:118-119](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/stimulus.v#L118-L119) —— `stim_done = (rd_state==STIM_DONE)`；`valid_packet = (CW==0) | mem_data[0]`，即当控制位宽 CW>0 时，用存储字最低位当「本行有效」标志。

> 旁注：仓库里还有一个更完整的 `oh_stimulus`（[stdlib/rtl/oh_stimulus.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_stimulus.v)），它把 `stimulus` 扩展成「读文件 / 随机 / 旁路」多模式（mode 驱动），并在内部用 `oh_dpram` 当存储体、用 `oh_random` 当随机源——它是 `stimulus` 的「加强版」。两者共享同一套 IDLE/ACTIVE/PAUSE/DONE 状态机思路。

**(c) oh_simctrl：生命周期指挥**

`oh_simctrl` 的参数把「可调旋钮」一次列清：

[stdlib/testbench/oh_simctrl.v:9-15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L9-L15) —— `TIMEOUT`（超时周期数）、`PERIOD_CLK/FASTCLK/SLOWCLK`（三路时钟周期）、`RANDOM_CLK/RANDOM_DATA`（是否随机化时钟/数据）。

mode 的语义在端口注释里给出：

[stdlib/testbench/oh_simctrl.v:22-22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L22-L22) —— `0=idle, 1=load, 2=go, 3=rng, 4=bypass`。

复位/启动序列用一个 `initial` 块串起整条时间线（注意它依赖 `clk_phase` 作为时间单位）：

[stdlib/testbench/oh_simctrl.v:59-73](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L59-L73)

```verilog
initial begin
   #(1)
   nreset = 'b0;                       // 复位拉低
   ...
   #(clk_phase * TIME_RESET)           // 保持复位一段时间
   nreset = 'b1;                       // 释放复位
   #(clk_phase * TIME_WAIT)
   mode = 3'b001;                      // load：加载激励
   #(clk_phase * TIME_LOAD)
   mode = gomode;                      // go(010) 或 rng(011)
end
```

`gomode` 由 `RANDOM_DATA` 决定走 stim 数据还是随机源（[L52-L57](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L52-L57)）。三路时钟各自用 `always #(...) clk = ~clk;` 翻转（[L98-L105](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L98-L105)）。

完成判结论：在 `posedge clk` 上等 `dut_done`，等 500 个时间单位后看 `dut_fail` 给出 PASSED/FAILED：

[stdlib/testbench/oh_simctrl.v:111-120](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L111-L120)

```verilog
always @ (posedge clk)
  if(dut_done) begin
     #500
     if(dut_fail) $display("[OH] DUT TEST FAILED");
     else         $display("[OH] DUT TEST PASSED");
     $finish;
  end
```

超时兜底是一个独立 `initial`，到点强制结束：

[stdlib/testbench/oh_simctrl.v:125-130](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L125-L130) —— `#(TIMEOUT) $display("[OH] DUT TEST TIMEOUT"); $finish;`。这正是 u4-l1「四件套范式」里「超时兜底」的落地。

最后，`sim.sh` 把 `.emf` 和 `dut.bin` 串起来：把传入的 `.emf` 软链成回放器期望的固定文件名 `test_0.emf`，再执行仿真：

[scripts/sim.sh:1-7](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/sim.sh#L1-L7)

```bash
if [ -L "test_0.emf" ]; then unlink test_0.emf; fi
ln -s $1 test_0.emf
./dut.bin
```

#### 4.2.4 代码实践：跟踪一次仿真生命周期

**实践目标**：不跑仿真，仅靠读源码，把 `oh_simctrl` 从上电到 `$finish` 的关键时间点排成一张表。

**操作步骤**：

1. 读 [oh_simctrl.v:59-73](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L59-L73) 与默认参数（`TIME_RESET=TIME_WAIT=TIME_LOAD=50`，`PERIOD_CLK=10`，`clk_phase=5`）。
2. 用 `clk_phase` 作时间单位，推算 nreset 释放、mode=load、mode=go 各自发生的绝对时刻。
3. 读 [oh_simctrl.v:111-130](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L111-L130)，确认 PASSED/FAILED 与 TIMEOUT 两条退出路径互不冲突。

**需要观察的现象**：mode 序列是 `0→1→(2 或 3)`；nreset 在 mode 变 load 之前就已释放；dut_done 与 TIMEOUT 谁先到谁生效。

**预期结果**：一张三列表格（时刻 / 事件 / 相关信号），覆盖复位、加载、启动、正常结束、超时五种事件。

**待本地验证**：若你想在波形里确认，可在 testbench 顶层 `$dumpvars` 后用 gtkwave 看 `nreset`、`mode`、`dut_done` 三条曲线的时间关系（环境搭建见 u1-l3）。

#### 4.2.5 小练习与答案

**练习 1**：`dv_driver` 里 `stim_done = &(stim_vec_done[N-1:0])` 用的是「与归约」而非「或归约」，为什么？
**答案**：因为「全部通道都回放完」才算整体完成——只要还有一条通道没 DONE，整体就不能算完。与归约正好表达「所有位都为 1 才为 1」；或归约会变成「任一通道完成即结束」，语义错误。

**练习 2**：`stimulus.v` 的状态机里，从 `STIM_ACTIVE` 转到 `STIM_DONE` 的条件是什么？
**答案**：`~stim_valid`，即读到的存储字被判为「无效」（当 CW>0 时即 `mem_data[0]==0`）。也就是说 `.emf` 里用一个「无效行」充当结束标记。

**练习 3**：`oh_simctrl` 里 `dut_done` 触发后为什么还要 `#500` 再判 `dut_fail`？
**答案**：给 DUT/监视器留出最后一拍的「收尾时间」——让最终响应、trace 落盘等异步收尾动作稳定下来，再去采样 fail 标志，避免在过渡瞬间误判。

---

### 4.3 随机事务生成器 egen.pl

#### 4.3.1 概念说明

手写 `.emf` 适合构造精确的小用例（如 4.1.4）；但当 DUT 是一个完整链路时，需要成百上千个、覆盖各种尺寸与对齐的随机事务来做压力测试。`egen.pl` 就是 OH! 自带的随机事务生成器：它按一个「字节预算」产出随机写事务，自动去重，再为每个写追加一个读回和一条期望值，从而自动得到「激励 + 黄金参考」一对文件。

它是 Perl 脚本（`#! /usr/bin/perl`），用 `Getopt::Long` 解析命令行参数，输出直接就是 `.emf` 行。

#### 4.3.2 核心流程

```
解析参数 (-mode/-n/-bl/-dstaddr/-srcaddr/-32/-c)
   │
   ├─ 循环直到累计字节数 ≥ n：
   │     ① 随机选尺寸（byte/half/word/double）→ ctrlmode
   │     ② 随机生成合法对齐的 dstaddr / returnaddr
   │     ③ 随机生成 datahi/datalo
   │     ④ 用 %usedaddr 哈希检查地址未被占用，避免写覆盖
   │     ⑤ 打印一行 WRITE，累计字节
   │
   └─ 收尾：对每个已记录的写事务，打印
         一行 READ（把写位清掉，ctrlmode & 0x6）
         一行 EXPECTED（写入时被掩码裁出的真实有效数据）
```

「写 + 读 + 期望」三件套是它的精髓：写的时候它已经记下「真正落进存储的有效数据」（受尺寸掩码约束），所以期望值行就是黄金参考，仿真后比对即可自动判错。

#### 4.3.3 源码精读

命令行接口与用法在文件开头的 Usage 里写得很清楚：

[emesh/dv/egen.pl:7-31](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L7-L31)

```
Usage: egen.pl  -mode <random|memcpy>
                -n    <number-of-bytes>
                -bl   <max-burst-in-bytes>
Example1: egen.pl -mode random -n 1024 -bl 128
Example2: egen.pl -mode memcpy -n 1024 -dstaddr 90000000 -srcaddr 80000000
```

参数解析（`-32` 把最大 ctrlmode 限制到 5，即只用 ≤32 位事务，不用 64 位双字）：

[emesh/dv/egen.pl:32-67](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L32-L67)，其中 `-32` 分支：`if(defined $opt_32){$ctrl_max=5;} else {$ctrl_max=7;}`。

随机模式下 ctrlmode 的生成——`int(rand(ctrl_max))+1` 再 `| 0x1` 强制置写位（所以 random 模式只产出写事务）：

[emesh/dv/egen.pl:93-96](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L93-L96)

```perl
if($mode eq "random"){
    $ctrlmode = (int(rand(hex($ctrl_max))) + 1) | 0x1;  # only writes
    $burst    = (int(rand($opt_bl)) + 1);               # variable bursts
}
```

尺寸→地址掩码/增量的分流，是 4.1.3 那张 ctrlmode 表的真正出处：

[emesh/dv/egen.pl:101-128](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L101-L128) —— `if($ctrlmode<2){...incr=1...} elsif($ctrlmode<4){...incr=2...} elsif($ctrlmode<6){...incr=4...} else{...incr=8...}`。

地址去重：用一个 Perl 哈希 `%usedaddr` 记下所有被某次写覆盖的字节地址，若新事务会撞上已有数据则跳过，避免「后写覆盖前写」导致期望值失真：

[emesh/dv/egen.pl:141-152](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L141-L152)

```perl
$inuse=0;
for($k=0;$k<$incr;$k++){
    $addr=$dstaddr+$k;
    if(exists ($usedaddr{$addr})){ $inuse=1; }
    $usedaddr{$addr}=1;
}
if($inuse == 0){ ...记录事务并打印 WRITE... }
```

写事务的打印（与 4.1.3 同一处）：

[emesh/dv/egen.pl:161-162](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L161-L162) —— 五段格式，第二段 datalo、第三段 dstaddr。

收尾的读回 + 期望值，是自动判错的依据：

[emesh/dv/egen.pl:175-190](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L175-L190)

```perl
# READ：把写位清掉（ctrlmode & 0x6），地址换成 returnaddr
printf("%08x_%08x_%08x_%02x_0000//READ\n",
       $transaction[$i]{returnaddr}, hex("0xDEADBEEF"),
       $transaction[$i]{dstaddr}, $transaction[$i]{ctrlmode}&hex(0x6));
# EXPECTED：写入时被掩码裁出的有效数据
printf("%08x_%08x_%08x_%02x\n",
       $transaction[$i]{reshi}, $transaction[$i]{reslo},
       $transaction[$i]{returnaddr}, $transaction[$i]{ctrlmode});
```

注意 READ 的第四段 `ctrlmode & 0x6`：`0x6 = 0110`，保留了 bits[2:1]（datamode）、清掉了 bit0（写位）——正是把「写」翻成「读」。

#### 4.3.4 代码实践：生成并解读随机测试

**实践目标**：用 `egen.pl` 生成一份小批量随机 `.emf`，逐类识别它输出的写/读/期望行。

**操作步骤**：

1. 进入 `emesh/dv/` 目录（脚本里有相对依赖与默认地址基）。
2. 运行（Perl 通常预装）：
   ```bash
   perl egen.pl -mode random -n 32 -bl 8 -32 > my_random.emf
   ```
   `-n 32` 表示累计写 32 字节即停；`-bl 8` 限制最大突发 8 字节；`-32` 限定只用 ≤32 位事务，输出更易读。
3. 用文本查看 `my_random.emf`，给每行标注它是 WRITE / READ 还是 EXPECTED。

**需要观察的现象**：

- WRITE 行第五段恒为 `0000`，第四段 ctrlmode 奇数（写位=1）。
- 每个 WRITE 之后（在文件末尾的读回区）对应一个 READ（ctrlmode 偶数）和一个 EXPECTED。
- 字节写（ctrlmode `01`）的 EXPECTED 第二段只有低 2 位十六进制非零；字写（`05`）的 EXPECTED 第二段是完整 8 位。
- 同一个地址不会在两次 WRITE 中重复出现（去重生效）。

**预期结果**：一份按「先全部 WRITE，再成对 READ/EXPECTED」组织的 `.emf`，地址两两不冲突，尺寸在字节/半字/字之间随机分布。

**待本地验证**：若机器无 Perl，可改为阅读 [emesh/dv/egen.pl:137-190](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L137-L190) 在脑中「单步执行」，手动推演 `-n 8 -32` 会输出哪几行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `egen.pl` 的 random 模式只生成写事务，读事务却出现在输出里？
**答案**：random 模式用随机数据「先写满一片存储」，再在收尾段（[L175-L190](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/dv/egen.pl#L175-L190)）为每次写追加一个读回和一条期望值。读是「验证手段」，不是随机激励本身。

**练习 2**：去掉 `-32` 后，输出可能多出哪类事务？
**答案**：ctrl_max 从 5 变 7，ctrlmode 可能取到 `0x07`（双字/64 位写），相应地址增量变 8、两段数据都被用满。

**练习 3**：`ctrlmode & 0x6` 这个操作具体改了哪些位？
**答案**：`0x6 = 0110`，按位与后保留 bits[2:1]（datamode），把 bit0（写位）清 0，bit3 及以上也清 0——即把一个「写」事务的控制码转成同尺寸的「读」事务控制码。

---

## 5. 综合实践

把本讲三件事（.emf 格式、回放/生命周期、随机生成）串起来，做一次「从生成到比对」的纸上推演：

1. 用 `egen.pl -mode random -n 16 -32` 生成一份 `.emf`（或手写 4 行写 + 4 行读，见 4.1.4）。
2. 从输出里任选一对 WRITE + 对应的 READ + EXPECTED，把三行的五个字段分别填进一张表。
3. 解释这三行的因果：WRITE 把数据写进 dstaddr；READ 向同一 dstaddr 发读、把返回地址放到第一段；EXPECTED 是 WRITE 时被尺寸掩码裁出的「应当读回」的值。
4. 结合 4.2，说明这三个事务分别由谁送出、由谁记录：WRITE/READ 由 `stimulus` 回放进 DUT，DUT 的应答由 `emesh_monitor` 记进 `test_0.trace`，最后 `diff test_0.trace <期望>` 完成自动判错。
5. 结合 `oh_simctrl`，标出这次仿真里 mode 会经历 `idle→load→go`，并在 `stim_done` 拉高后由 `dut_done` 触发 PASSED/FAILED。

**交付物**：一张「字段解码表」+ 一条「事务在测试平台中的流转路径」文字说明。运行部分若环境未就绪，标注「待本地验证」即可。

## 6. 本讲小结

- `.emf` 一行一个事务，五段十六进制：`srcaddr/datahi _ datalo _ dstaddr _ ctrlmode _ access`，字段定义以 `egen.pl` 的 `printf` 为权威。
- `ctrlmode` 的 bit0 是写位、bits[2:1] 是 datamode（00/01/10/11 = 字节/半字/字/双字），字节数满足 \(8 \ll \text{datamode}\)。
- `dv_driver` 是「回放 + 监视 + 存储」三件套容器，按通道 `generate` 展开，全局完成信号是各通道 done 的与归约——但其 `stimulus`/`ememory` 实例与当前仓库存在接口漂移，属架构意图。
- 真正加载 `.emf` 的是 `stimulus.v`：`$readmemh` 一次读入，IDLE→ACTIVE→PAUSE→DONE 状态机按 `dut_ready` 节拍回放，遇无效行即停。
- `oh_simctrl` 用一个 `initial` 串起「复位 → load → go」时间线，在 `dut_done` 后判 PASSED/FAILED，并用独立 `initial` 做 TIMEOUT 兜底。
- `egen.pl` 按字节预算生成随机写、用哈希去重，再为每写追加 READ 与 EXPECTED，自动产出「激励 + 黄金参考」。

## 7. 下一步学习建议

- **进入 emesh 协议**：本讲只把 `.emf` 当成「五段事务」来用，刻意没展开 104 位包的精确位序。下一步请学 **u5-l1 emesh 包格式与协议**，把 `.emf` 的字段与 104 位总线上 `ctrlmode/datamode/write/access/dstaddr/srcaddr/data` 的物理位对应起来。
- **动手接一个 DUT**：学完 emesh 后，回到 **u4-l3 编写你的第一个 DUT 测试**，把一个真实 IP（如 gpio）接进测试平台，端到端跑通「写 `.emf` → 回放 → trace 比对」。
- **读加强版回放器**：若你对多模式激励感兴趣，可对比阅读 [stdlib/rtl/oh_stimulus.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_stimulus.v)，看它如何用 mode 在「读文件 / 随机 / 旁路」之间切换，并与本讲的 `stimulus.v` 互相印证。
