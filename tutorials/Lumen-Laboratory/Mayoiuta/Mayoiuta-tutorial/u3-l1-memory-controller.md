# 存储控制器与多 Bank 设计

## 1. 本讲目标

本讲进入 NPU 的「存储侧」。在前两单元里，我们看完了核内怎么算（PE 阵列、卷积引擎、自适应精度 PE），但这些计算单元的输入数据从哪来、算完的结果写到哪去？答案就是**存储控制器（Memory Controller）**。

学完本讲，你应当能够：

- 看懂 `mem_ctl.v` 里 `memory_bank` 这个**二维数组**是如何把一大块存储切成 8 个独立「银行（bank）」的。
- 解释一条 32 位地址是如何被**解码**成「选哪个 bank」+「bank 内第几个字」两段的。
- 说出**主机（host）写**和**NPU 读**这两条端口在同一时钟周期里访问不同 bank 时为什么不会打架。
- 识别 `ecc_checker` 是一个仓库**未提供源码**的外部例化模块，并指出它当前接线里的可疑之处。

---

## 2. 前置知识

本讲假设你已学完 u1-l3（顶层 SoC），知道 `NPU_SOC` 把存储、计算、互连挂在一起。下面补充几个本讲要用到的存储相关术语，全部用大白话解释。

### 2.1 什么是 bank（存储体）

把一块大存储想象成一栋大楼，**bank** 就是楼里的「房间」。如果把所有数据堆在一个房间里，每次只能有一个人进出（一次只能读写一个地址）。把它切成 8 个房间后，8 个房间可以**同时**各进各的，吞吐量就上去了。NPU 每个时钟周期都要喂给 PE 阵列大量数据，单 bank 根本来不及供，所以必须分 bank。

### 2.2 地址解码（address decode）

CPU 给出一条地址，存储器要回答两件事：**去哪个 bank？** 进了 bank 之后**找第几个字？** 这就是把一条地址切成两段的过程，叫地址解码。

### 2.3 双端口存储（dual-port）

普通存储一条地址线、一条数据线，一个周期只能做一件事。**双端口存储**有两套独立的地址/数据引脚，可以同时进行两次访问（比如一边写、一边读）。本讲的控制器就是用 Verilog 描述一个双端口行为。

### 2.4 ECC（错误纠正码）

存储里的数据有可能被噪声翻转（俗称「比特翻转」）。**ECC** 是一种在写入时多存几位校验位、读出时检查甚至纠正错误的机制。`syndrome`（伴随式）就是 ECC 读出时算出来的「错误指纹」——全 0 表示没错，非 0 指示哪一位翻 了。

### 2.5 非阻塞赋值的一句话回顾

Verilog 时序逻辑里用 `<=`（非阻塞赋值）：在一个时钟沿，等这个沿所有右侧的值都读完后，才统一更新左侧。这一点在本讲讲「同周期又读又写同一个 bank」时很关键。

> 名词速查：NPU、RTL、Verilog module、顶层模块、CU/MU/IN/CU 划分、generate-for 在前面几讲已建立，本讲直接使用，不再重复。

---

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它牵出一个仓库里**没有**的模块，这是本讲的一个重要观察点。

| 文件 | 作用 | 本讲如何使用 |
|---|---|---|
| [mem_ctl.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v) | `Memory_Controller` 模块：多 bank 存储、双接口、地址解码、ECC 例化 | 逐行精读 |
| README.md | 第 29 行把 MU（Memory Unit）定位为「存放权重、激活值、中间结果」 | 用于确认本模块在系统里的职责 |
| `ecc_checker`（无源码） | 被例化的 ECC 校验模块 | 仓库未提供，标注「待确认」 |

先看模块全貌。整个 `Memory_Controller` 只有一段参数、一组端口、一个数组、一个 `always` 块、一个 ECC 例化，非常紧凑：

[mem_ctl.v:1-17](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L1-L17) — 声明模块名 `Memory_Controller`、三个参数（`ADDR_WIDTH/DATA_WIDTH/BANK_NUM`）与两组端口（主机接口、NPU 接口）。

下面按机制拆成四个最小模块来讲。

---

## 4. 核心概念与源码讲解

### 4.1 多 Bank 存储体的组织：memory_bank 二维数组

#### 4.1.1 概念说明

我们要描述的物理对象是「一块很大的存储」。在 Verilog 里，一块存储最自然的写法是用一个**数组**：数组的每个元素是一个字（word），字宽是 `DATA_WIDTH`。本讲的关键设计是把这个数组做成**二维**的：

- 第一维：**bank 编号**（0 到 `BANK_NUM-1`）。
- 第二维：**该 bank 内的字地址**。

这样 `memory_bank[b][w]` 就表示「第 b 个 bank 的第 w 个字」。把它画成表格就是 8 行（8 个 bank），每行很多列（每行内部深度）。这种二维数组的本质，就是把 8 个独立的小存储「绑」在一个变量名下，让后面的逻辑可以**用地址的几位直接选 bank**。

#### 4.1.2 核心流程

存储的组织流程可以概括为三步：

1. **切容量**：把全部可寻址空间按 bank 数等分，每个 bank 负责一段。
2. **声明数组**：用二维 `reg` 数组一次性描述所有 bank 的所有字。
3. **按地址读写**：用地址的若干低位作 bank 下标、其余位作字下标，定位到具体那个字。

每个 bank 的深度（能存多少字）由「地址位数减去 bank 选择的位数」决定，写成公式：

\[
\text{每个 bank 的深度} = 2^{\text{ADDR_WIDTH} - \lceil \log_2(\text{BANK_NUM}) \rceil}
\]

本设计中 `BANK_NUM = 8`，所以 \(\lceil \log_2(8) \rceil = 3\)，即用 3 位选 bank。

#### 4.1.3 源码精读

[mem_ctl.v:19](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L19) — 声明二维存储数组：

```verilog
reg [DATA_WIDTH-1:0] memory_bank [0:BANK_NUM-1][0:(1<<(ADDR_WIDTH-3))-1];
```

逐段拆这句话：

- `reg [DATA_WIDTH-1:0]`：每个字是 `DATA_WIDTH`（=256）位宽。
- `[0:BANK_NUM-1]`：第一维，bank 编号 0..7（共 8 个 bank）。
- `[0:(1<<(ADDR_WIDTH-3))-1]`：第二维，每个 bank 内的字数。`1<<(32-3)` = `1<<29` = \(2^{29}\) 个字。

> 关键直觉：`ADDR_WIDTH-3` 里的「3」正是 \(\log_2(8)\)。也就是说「用来选 bank 的 3 位」从地址空间里被扣掉了，剩下的位才用来在 bank 内寻址。这个 3 是和 `BANK_NUM=8` 强绑定的——若改 `BANK_NUM`，这里也要同步改，否则维度和后面的解码会对不上（这是一个潜在的维护坑，值得留意）。

#### 4.1.4 代码实践

**目标**：验证「改参数 → bank 深度跟着变」的直觉。

1. 打开 [mem_ctl.v:19](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L19)。
2. 在脑中（不要真改源码）把 `ADDR_WIDTH` 从 32 改成 16，保持 `BANK_NUM=8`。
3. 手算新的第二维上界：`1<<(16-3) - 1` = \(2^{13}-1\) = 8191，即每个 bank 8192 个字。
4. **需要观察的现象**：bank 数不变（仍是 8），但每个 bank 的字数从 \(2^{29}\) 缩到 \(2^{13}\)，总容量大幅下降。
5. **预期结果**：体会到「地址位宽直接决定每个 bank 的深度」，以及第 19 行那个 `3` 是写死的、与 `BANK_NUM` 不联动的设计弱点。本仓库无仿真脚手架，运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `BANK_NUM` 改成 4，第 19 行的 `ADDR_WIDTH-3` 应该改成什么，才让维度与解码自洽？

**答案**：改成 `ADDR_WIDTH-2`。因为 \(\log_2(4)=2\)，要用 2 位选 bank，剩下 `ADDR_WIDTH-2` 位作字地址。这也暴露了当前代码把 `3` 写死、没有写成 `\($clog2(BANK_NUM)\)` 的不足。

**练习 2**：`memory_bank` 的每个字多少字节？

**答案**：`DATA_WIDTH=256` 位 = 256/8 = 32 字节。

---

### 4.2 地址解码：bank 选择 + bank 内偏移

#### 4.2.1 概念说明

有了二维数组，接下来要回答：给定一条 32 位地址，怎么落到 `memory_bank[b][w]` 上？这就是**地址解码**。本设计用的是**低位交叉（low-order interleaving）**：用地址的**最低几位**选 bank，用**高位**作 bank 内偏移。

为什么用低位选 bank？因为程序/数据访问通常是**连续地址**，连续地址在二进制下是最低位变化最快的。用低位选 bank，意味着连续地址会**轮流落在不同 bank** 上，从而让「顺序读一串数据」天然分散到多个 bank，并行吞吐最大化。这正合 NPU 成块搬权重/激活的需求。

#### 4.2.2 核心流程

一条 `host_addr`（32 位）被切成两段：

\[
\text{bank 编号} = \text{host\_addr}[2{:}0] \quad (\text{低 3 位})
\]

\[
\text{bank 内字地址} = \text{host\_addr}[\text{ADDR\_WIDTH}-1{:}3] \quad (\text{高 29 位})
\]

校验一下：3 位 bank + 29 位字地址 = 32 位，正好覆盖整条 `host_addr`，没有空洞也没有重叠。访问流程：

1. 取地址低 3 位 → 决定打开第几个 bank。
2. 取地址高 29 位 → 在该 bank 内定位字。
3. 对 `memory_bank[bank][word]` 读或写。

#### 4.2.3 源码精读

[mem_ctl.v:23](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L23) — 主机写时用同一套解码定位写入点：

```verilog
memory_bank[host_addr[2:0]][host_addr[ADDR_WIDTH-1:3]] <= host_wr_data;
```

- `host_addr[2:0]`：低 3 位选 bank。
- `host_addr[ADDR_WIDTH-1:3]` 即 `host_addr[31:3]`：高 29 位选 bank 内字。

[mem_ctl.v:25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L25) 与 [mem_ctl.v:28](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L28) — 主机读和 NPU 读用**完全相同**的解码方式，只是把 `host_addr` 换成 `npu_addr`：

```verilog
host_rd_data <= memory_bank[host_addr[2:0]][host_addr[ADDR_WIDTH-1:3]];
npu_rd_data  <= memory_bank[npu_addr[2:0]][npu_addr[ADDR_WIDTH-1:3]];
```

> 关键直觉：两套端口共享**同一块** `memory_bank`，靠各自的地址独立解码。只要两边地址的低 3 位不同（落在不同 bank），就能在同一周期各取各的，互不干扰——这正是多 bank 的全部价值所在。

#### 4.2.4 代码实践

**目标**：手工解码一条地址，确认「哪个 bank、哪个字」。

1. 设 `host_addr = 32'h0000_0009`（十进制 9）。
2. 取低 3 位：`9 & 0b111` = `0b001` = **bank 1**。
3. 取高 29 位：`9 >> 3` = `1`，即 bank 1 内的**第 1 个字**。
4. **需要观察的现象**：连续地址 8（bank 0, 字 1）、9（bank 1, 字 1）、10（bank 2, 字 1）……确实轮流落进不同 bank，印证低位交叉。
5. **预期结果**：地址每加 1 就换一个 bank，每加 8 才回到同一 bank 的下一个字。这是「低位交叉提升顺序带宽」的直接体现。

#### 4.2.5 小练习与答案

**练习 1**：`host_addr = 32'h0000_0010`（十进制 16）落在哪个 bank、哪个字？

**答案**：低 3 位 `16 & 7 = 0` → bank 0；高 29 位 `16 >> 3 = 2` → 字 2。即 `memory_bank[0][2]`。

**练习 2**：为什么本设计用「低位选 bank」而不是「高位选 bank」？

**答案**：低位选 bank 时，连续地址天然分散到不同 bank，顺序访问能获得多 bank 并行带宽，适合 NPU 成块搬数据；若用高位选 bank，连续地址会挤在同一个 bank 里，丧失并行优势，只适合「按大块分区域」的用途。

---

### 4.3 主机写 / NPU 读的双接口与同周期并发

#### 4.3.1 概念说明

`Memory_Controller` 对外暴露**两套端口**，扮演两种角色：

- **主机接口**：CPU/主机这一侧。主机负责把权重和输入数据**写进**存储（`host_wr_en` / `host_wr_data`），也可以回读（`host_rd_data`）。
- **NPU 接口**：计算核这一侧。NPU 只**读**存储（`npu_rd_en` / `npu_rd_data`），把数据喂给 PE 阵列/卷积引擎。

这对应 README 里 MU 的职责——「存放权重、激活值、中间结果」。主机把数据灌进来，NPU 算的时候再读出去。两套端口共享同一块 `memory_bank`，于是核心问题变成：**它们会不会撞车？**

#### 4.3.2 核心流程

整个读写被写在**同一个**时钟沿触发的 `always` 块里，分三段：

1. **主机写**（条件 `host_wr_en`）：把 `host_wr_data` 写进 `host_addr` 解码出的字。
2. **主机读**（无条件）：把 `host_addr` 解码出的字读到 `host_rd_data`。
3. **NPU 读**（条件 `npu_rd_en`）：把 `npu_addr` 解码出的字读到 `npu_rd_data`。

并发行为分两种情况：

- **不同 bank**：主机写 bank A、NPU 读 bank B。两段访问的是 `memory_bank` 数组里**不同的元素**，互不影响，同一周期并行完成——这就是多 bank 设计的收益。
- **同一 bank（甚至同一字）**：由于用的是非阻塞赋值 `<=`，NPU 读到的是**时钟沿开始时的旧值**，主机写的新值要到下个周期才生效。即「读旧值、写下个周期生效」（read-old-data）语义，不会出现结构冒险。

#### 4.3.3 源码精读

[mem_ctl.v:21-30](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L21-L30) — 整个读写时序逻辑，三段都在一个时钟沿里：

```verilog
always @(posedge clk) begin
    if (host_wr_en) begin
        memory_bank[host_addr[2:0]][host_addr[ADDR_WIDTH-1:3]] <= host_wr_data;
    end
    host_rd_data <= memory_bank[host_addr[2:0]][host_addr[ADDR_WIDTH-1:3]];

    if (npu_rd_en) begin
        npu_rd_data <= memory_bank[npu_addr[2:0]][npu_addr[ADDR_WIDTH-1:3]];
    end
end
```

读这段时有三个值得记的细节：

- **主机读没有读使能**：[mem_ctl.v:25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L25) 不在任何 `if` 里，每个时钟沿都无条件把 `host_addr` 指向的字搬到 `host_rd_data`。也就是说主机读端口是「常开」的，`host_rd_data` 永远滞后一拍地反映当前 `host_addr` 的内容。
- **NPU 读有读使能**：[mem_ctl.v:27-29](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L27-L29) 受 `npu_rd_en` 控制；不读时 `npu_rd_data` 保持上一次的值（寄存器不更新）。
- **`rst_n` 声明了却没用**：[mem_ctl.v:7](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L7) 的 `rst_n` 在整个 `always` 块里**没有任何复位分支**。这意味着上电后存储内容和 `host_rd_data/npu_rd_data` 的初值是未知（`X`），直到主机写入有效数据。这是真实设计里通常要补的待确认点。

> 关于综合：一段时钟沿 `always` 块、对同一个数组有两套独立地址的读写，综合工具通常会把它推断成一块**双端口块 RAM**（host 一口、NPU 一口）。这是本设计能用「一个数组 + 一个 always」描述双端口的底层原因。

#### 4.3.4 代码实践

**目标**：跟踪一次「主机写 bank 2、NPU 同时读 bank 5」的同周期行为。

1. 设某周期 `host_wr_en=1, host_addr=32'h0000_0012`（低 3 位 `2` → bank 2），`host_wr_data=D`；同时 `npu_rd_en=1, npu_addr=32'h0000_001D`（低 3 位 `5` → bank 5）。
2. 跟踪 [mem_ctl.v:23](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L23)：写 `memory_bank[2][...] <= D`。
3. 跟踪 [mem_ctl.v:28](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L28)：读 `memory_bank[5][...]` 进 `npu_rd_data`。
4. **需要观察的现象**：两段访问的 bank 下标分别是 2 和 5，互不重叠；写和读在同一个时钟沿各自完成，`npu_rd_data` 在下个周期变成 bank 5 的内容。
5. **预期结果**：确认「不同 bank 可同周期并发」；再设想两者都访问 bank 2 的同一字，应观察到 NPU 读到的是**旧值**（非阻塞赋值语义）。本仓库无 testbench，运行波形**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `host_rd_data` 不加读使能也能工作？有什么副作用？

**答案**：因为它每个周期都自动跟随 `host_addr` 更新，主机只要改 `host_addr`、下个周期就能在 `host_rd_data` 拿到数据，省了一根使能线。副作用是 `host_rd_data` 在地址切换的中间周期会短暂出现「过渡值」，主机端需要自己判断何时采样才有效。

**练习 2**：主机写 bank 2 的某字、NPU 同周期读 bank 2 的同一字，NPU 拿到的是新值还是旧值？为什么？

**答案**：旧值。因为写和读都用 `<=`（非阻塞赋值），在同一个时钟沿，右侧先统一取值（读到的是写之前的旧内容），左侧再统一更新。新写入的值要到下个时钟周期才能被读到。

---

### 4.4 ECC 校验的例化：ecc_checker（待确认）

#### 4.4.1 概念说明

存储里存放的是权重和激活，万一某个比特翻转，整次推理就错了。**ECC（Error Correction Code）** 的思路是：写入时多算几位校验位一起存，读出时重新计算并对比，能发现甚至纠正错误。本模块在末尾例化了一个叫 `ecc_checker` 的子模块，意图就是给存储加一层 ECC 保护。

但必须立刻说明：**`ecc_checker` 的源码在仓库里并不存在**（`git ls-files` 列出的 9 个 `.v` 文件里没有它）。所以它的内部行为——到底能纠几位、`syndrome` 是几位、`data_in/data_out` 的方向——全部**待确认**。我们只能从它在 `mem_ctl.v` 里的接线推测设计意图。

#### 4.4.2 核心流程

从接线推测的预期流程是：

1. 主机写入 `host_wr_data` 时，`ecc_checker` 同时拿到这份待写数据（`data_in`），本应生成校验位一起存进 bank。
2. 主机读出 `host_rd_data` 时，`ecc_checker` 本应检查这份数据（`data_out`），算出 `syndrome` 指示是否有错。
3. 上层根据 `syndrome` 决定是否重读或纠正。

注意：以上是「设计意图」，并非仓库中验证过的行为。

#### 4.4.3 源码精读

[mem_ctl.v:32-39](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L32-L39) — 例化 ECC 检查器：

```verilog
// ECC校验
ecc_checker #(
    .DATA_WIDTH(DATA_WIDTH)
) u_ecc (
    .data_in(host_wr_data),
    .data_out(host_rd_data),
    .syndrome()
);
```

逐行读：

- `.DATA_WIDTH(DATA_WIDTH)`：把数据位宽（256）传给子模块。
- `.data_in(host_wr_data)`：把主机**待写**数据接进检查器。
- `.data_out(host_rd_data)`：把检查器输出接到主机**读回**数据上。
- `.syndrome()`：伴随式端口**悬空**（空括号），没人接收。

这段接线有两处明显的可疑，必须标注为待确认：

1. **`syndrome()` 悬空**：ECC 的核心结果就是 `syndrome`，这里却什么都不接，等于「检查了但没把结果交出去」。即便 `ecc_checker` 内部算得再正确，上层也永远拿不到错误信息。这让这次例化在功能上接近「空转」。
2. **`host_rd_data` 可能有多驱动**：[mem_ctl.v:12](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L12) 把 `host_rd_data` 声明为 `output reg`，[mem_ctl.v:25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L25) 的 `always` 块已经驱动了它；而这里 `.data_out(host_rd_data)` 又让 `ecc_checker` 试图驱动同一根线。若 `data_out` 是子模块的输出端口，就构成**多驱动冲突**（multiple drivers）。由于 `ecc_checker` 源码缺失，端口方向无法确认，结论暂记为**待确认**。

> 综合判断：这个 `ecc_checker` 例化更像是一个「占位/骨架」——它在文件里占了个位置，表示「这里将来要接 ECC」，但当前的接法既丢掉了 `syndrome`，又有潜在多驱动，并没有真正参与存储的读写纠错。这正是源码阅读型项目里常见的「半成品」，要如实指出，不能当成可用功能来讲解。

#### 4.4.4 代码实践

**目标**：把 `ecc_checker` 当黑盒，仅从接线推断它「应该」做什么、当前「实际」做了什么。

1. 阅读 [mem_ctl.v:33-39](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L33-L39)。
2. 列出它的三个端口连接：`data_in ← host_wr_data`、`data_out → host_rd_data`、`syndrome → 空`。
3. **需要观察的现象**：`syndrome` 悬空、`data_out` 与 `always` 块共驱动 `host_rd_data`。
4. **预期结果**：得出结论——当前 ECC 在功能上不生效（结果丢失 + 潜在多驱动），属于占位实现。尝试在仓库里 `grep ecc_checker`，确认除本文件外没有任何模块定义它，印证「外部待确认」。
5. 本结论基于接线推断，`ecc_checker` 真实端口方向**待确认**。

#### 4.4.5 小练习与答案

**练习 1**：从功能角度看，当前 `ecc_checker` 的例化能纠正存储里的比特错误吗？为什么？

**答案**：不能。因为 ECC 的判定结果 `syndrome` 被悬空（`.syndrome()`），没有任何逻辑接收它，上层无法得知是否出错，更谈不上纠正。这只是一次占位例化。

**练习 2**：如果由你来补全这个 ECC 设计，`syndrome` 应该接到哪里？

**答案**：至少应把 `syndrome` 接到一个输出端口（或状态寄存器），让主机/控制器能读到「是否出错、错在哪一位」，再配合 `data_out` 给出纠正后的数据，并把 `host_rd_data` 的驱动权统一交给一处（避免多驱动）。具体协议待 `ecc_checker` 源码确认后才能定型。

---

## 5. 综合实践

本实践把本讲的容量计算与并发行为串起来，是本讲规格里要求的核心任务。

### 5.1 计算每个 bank 的深度与总容量

给定默认参数 `ADDR_WIDTH=32, DATA_WIDTH=256, BANK_NUM=8`：

1. **选 bank 的位数**：\(\log_2(8)=3\) 位。
2. **每个 bank 的深度（字数）**：

\[
\text{depth} = 2^{\text{ADDR\_WIDTH}-3} = 2^{32-3} = 2^{29} = 536{,}870{,}912 \text{ 字}
\]

3. **每个字的字宽**：`DATA_WIDTH=256` 位 = 32 字节。
4. **单个 bank 容量**：

\[
2^{29} \times 32\text{ B} = 2^{29} \times 2^{5}\text{ B} = 2^{34}\text{ B} = 16\text{ GiB}
\]

5. **总容量（8 个 bank）**：

\[
8 \times 16\text{ GiB} = 2^{3} \times 2^{34}\text{ B} = 2^{37}\text{ B} = 128\text{ GiB}
\]

> **重要观察（待确认/可疑）**：默认参数描述的是一块 **128 GiB** 的存储，这显然**不可能**在 RTL 里综合成片上 SRAM——任何 FPGA/ASIC 都放不下。这说明 `ADDR_WIDTH=32` 在这里只是**示意性参数**，真实 NPU 的片上 scratchpad 会用小得多的地址位宽（比如 12~16 位），大容量数据则走片外 DDR（顶层 `npu_soc.v` 的 `ddr_data` 接口就是干这个的）。换句话说：解码**逻辑是对的**，但默认**容量参数不现实**，这是阅读时必须分清的两件事。

### 5.2 同周期主机写 / NPU 读不同 bank 的行为

- **场景**：某时钟沿 `host_wr_en=1` 写 bank 2，同时 `npu_rd_en=1` 读 bank 5。
- **行为**：写落在 [mem_ctl.v:23](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L23) 的 `memory_bank[2][...]`，读落在 [mem_ctl.v:28](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L28) 的 `memory_bank[5][...]`。两者访问数组的不同元素，无冲突并行完成，`npu_rd_data` 下一周期更新为 bank 5 的内容。
- **结论**：多 bank 的全部意义，就是让「主机灌数据」和「NPU 取数据」这两条流量在落到不同 bank 时**互不阻塞**，从而把存储带宽近似翻倍。

### 5.3 动手验证建议

1. 用上面的低位交叉规律，填一张「地址 → bank / 字」对照表（地址 0~15 即可）。
2. 标出哪些地址两两落在不同 bank（可同周期并发），哪些落在同一 bank（需排队或读旧值）。
3. 本仓库不含仿真脚手架，若要看到真实波形，需自行编写 testbench（实例化 `Memory_Controller`，给定 `host_addr/host_wr_data/host_wr_en` 与 `npu_addr/npu_rd_en`，观察 `host_rd_data/npu_rd_data`）——这属于**待本地验证**。

---

## 6. 本讲小结

- `Memory_Controller` 用一个**二维 `reg` 数组** `memory_bank[bank][word]` 把存储切成 8 个独立 bank，每个字 256 位（[mem_ctl.v:19](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L19)）。
- 地址解码采用**低位交叉**：`addr[2:0]` 选 bank，`addr[31:3]` 作 bank 内字地址（[mem_ctl.v:23](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L23)），让连续地址天然分散到多 bank。
- 对外是**双接口**：主机写/读、NPU 只读，共享同一块 `memory_bank`；落在不同 bank 时可同周期并发，落同一 bank 时因非阻塞赋值呈现「读旧值」语义（[mem_ctl.v:21-30](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v#L21-L30)）。
- `host_rd_data` **无读使能**、每拍自动跟随地址更新；`rst_n` 声明却**未使用**，上电初值为 `X`。
- 默认参数对应 **128 GiB** 的不现实容量——解码逻辑正确，但参数仅示意，真实片上存储会小得多，大容量走片外 DDR。
- `ecc_checker` 是仓库**未提供源码**的外部模块；其 `syndrome` 悬空、`data_out` 与 `always` 块疑似共驱动 `host_rd_data`，当前更像占位实现，全部**待确认**。

---

## 7. 下一步学习建议

存储控制器把数据「存」好了，但 NPU 计算时对数据的排布往往有特殊要求（比如 PE 阵列要按行喂、卷积要按窗口取）。下一讲 **u3-l2 数据格式重排 Data Reorder** 会讲 `data_reorder.v`：它就接在存储之后，负责把 NCHW / NHWC / Blocked 等不同排布的数据**重新摆放**成计算单元想要的形状。建议接着阅读 `hardware/rtl/memory/data_reorder.v`，并思考：从 `Memory_Controller` 的 `npu_rd_data` 流出的 256 位数据，应该按什么顺序送进 `Data_Reorder`？

如果想从系统角度看清存储在整个数据通路里的位置，可以先跳读 [npu_soc.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/top/npu_soc.v)（u1-l3 已讲过），再回到 u3-l2。
