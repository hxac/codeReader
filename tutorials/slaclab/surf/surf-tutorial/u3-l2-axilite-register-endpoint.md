# AXI-Lite 寄存器端点模式（helper 过程）

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 SURF 在双进程寄存器块里实现「内存映射寄存器」的标准四步骨架：`axiSlaveWaitTxn` → `axiSlaveRegister` / `axiSlaveRegisterR` → `axiSlaveDefault`。
- 看懂 `axiSlaveWaitTxn` 如何把一次 AXI-Lite 事务解码进一个 `AxiLiteEndpointType` 端点变量，并用 `writeEnable/readEnable` 标记「本拍要服务」。
- 区分 `axiSlaveRegister`（读/写寄存器）与 `axiSlaveRegisterR`（只读状态寄存器）的差异，并理解只读是怎么靠 `constVal="X"` 关掉写通路实现的。
- 解释 `axiSlaveDefault` 如何靠「有没有人把 `awready/arready` 拉高」判断地址未被映射，并对未映射访问返回 `AXI_RESP_DECERR_C`。
- 理解写副作用必须在 `comb` 的次态逻辑里显式表达，以及为什么跨时钟域的状态必须先同步（典型用 `AxiLiteAsync`）再暴露到 AXI-Lite。

本讲是 [u3-l1](u3-l1-axilite-records.md) 的直接延续：上一讲建立了 AXI-Lite 的四个记录类型与 `_INIT_C` 初值，本讲把这些记录「用起来」，搭出一个能被 CPU 读写的寄存器块。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：AXI-Lite 事务是「一次地址 + 一次数据」的握手。** AXI-Lite 把一次访问拆成 5 个通道（读：AR/R；写：AW/W/B），但 SURF 的从机 helper 把它简化成一个「单拍服务窗口」：当主机把 `awvalid&wvalid`（写）或 `arvalid`（读）拉起、且上一笔事务已经结清时，从机在这一拍「接单」——读出地址、读出（或写入）数据、并给出响应。helper 用 `writeEnable/readEnable` 两个单拍脉冲告诉你「现在这拍有一笔写/读要处理」。

**直觉二：寄存器块 = 一张「地址 → 变量」对照表。** 写一个 AXI-Lite 从机，本质就是回答两个问题：（a）这个地址归谁管？（b）归它管的话，读返回什么、写改什么。SURF 的做法是：用 `axiSlaveWaitTxn` 先判定「有没有事务、是读还是写」，然后用一连串 `axiSlaveRegister(axilEp, addr, offset, reg)` 把每个地址绑定到一个 `RegType` 里的变量，最后用 `axiSlaveDefault` 给「没人认领」的地址兜底。这套写法把繁琐的通道握手藏在包里，让你只写「地址映射表」。

还需要回忆 [u1-l5](u1-l5-two-process-style.md) 的双进程骨架：状态放 `RegType`，初值放 `REG_INIT_C`，`comb` 进程里 `v := r` 算次态、`rin <= v`，`seq` 进程只打寄存器。本讲的 AXI-Lite 逻辑全部写在 `comb` 里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [axi/axi-lite/rtl/AxiLitePkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd) | AXI-Lite 总包。定义端点类型 `AxiLiteEndpointType`、状态 `AxiLiteStatusType`、响应码，以及本讲的主角——一簇 `axiSlave*` helper 过程（声明在包里，实现在包体里）。 |
| [axi/axi-lite/rtl/AxiVersion.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd) | 标准范例从机：暴露固件版本、git hash、上电时间、scratchpad 等寄存器。本讲用它演示完整的「WaitTxn → Register/RegisterR → Default」调用链。 |
| [axi/axi-stream/rtl/AxiStreamTimer.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTimer.vhd) | 更小的范例从机，用于演示「写副作用」（`runCmd` 触发状态机）和「跨域先同步」（`AxiLiteAsync`）。 |
| [AGENTS.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md) | 贡献者「宪法」。其中的 *AXI-Lite Register Implementation Pattern* 一节正是本讲四步骨架的成文规定。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，前三个对应骨架的三步，第四个讲两件最容易踩坑的事：写副作用与跨域同步。

### 4.1 WaitTxn 解码：把一次事务装进端点

#### 4.1.1 概念说明

`axiSlaveWaitTxn` 是整个端点模式的「入口」。它做三件事：

1. 把当前 `comb` 进程里那份「正在被改写」的从机记录（`v.axiWriteSlave / v.axiReadSlave`）连同主机输入一起，拷进一个临时端点变量 `axilEp : AxiLiteEndpointType`；
2. 判定本拍是否有一笔需要服务的写或读事务，结果写进 `axilEp.axiStatus.writeEnable / readEnable`；
3. 把 `axilEp` 里的 `awready/wready/arready` 复位为 `'0'`，为后续「谁认领谁把它拉高」做好准备。

之所以要引入一个端点变量，是因为后面的 `axiSlaveRegister` / `axiSlaveDefault` 都要既读主机信号、又改从机信号，把这一堆信号打包成一个 `inout` 变量传进去，比每次都传六七个参数干净。这就是 AGENTS.md 里「keep AXI-Lite read/write slave records in `RegType`」与「Use `axiSlaveWaitTxn(...)` once near the start」两条规定的落地。

#### 4.1.2 核心流程

写通道与读通道各有一个底层「等待」过程，`axiSlaveWaitTxn`（端点版）只是把它们组合起来：

```
axiSlaveWaitTxn(ep, wrMaster, rdMaster, wrSlave, rdSlave):
    ep := AXI_LITE_ENDPOINT_INIT_C          # 清零，awready/wready/arready = '0'
    把 wrMaster/rdMaster/wrSlave/rdSlave 拷进 ep
    axiSlaveWaitWriteTxn(...)                # 算 ep.axiStatus.writeEnable
    axiSlaveWaitReadTxn(...)                 # 算 ep.axiStatus.readEnable
```

写通道的判定逻辑（简化）：

```
若上一拍已接单(awready&wready)：本拍置 bvalid='1'（给写响应）
否则若 awvalid&wvalid&~bvalid：writeEnable := '1'（本拍接一单新写）
末尾：awready := '0'; wready := '0'   # 默认未认领
```

读通道同理：上一拍 `arready` 则本拍置 `rvalid`；否则 `arvalid&~rvalid` 时 `readEnable := '1'`，末尾 `arready := '0'`。关键点是 `writeEnable/readEnable` 是「单拍服务脉冲」——只有在事务被接受的那一拍为 `'1'`，后续 `axiSlaveRegister` 正是靠它知道「现在要不要处理我这段地址」。

#### 4.1.3 源码精读

端点类型与状态类型定义：

[AxiLitePkg.vhd:180-207](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L180-L207) —— `AxiLiteStatusType`（只有 `writeEnable/readEnable` 两个比特）、`AxiLiteEndpointType`（把两套主/从记录 + 状态打包）以及 `AXI_LITE_ENDPOINT_INIT_C` 初值。这个端点记录就是把上一讲的四个记录「捆成一捆」再加一个状态位。

端点版 `axiSlaveWaitTxn` 的实现：

[AxiLitePkg.vhd:736-753](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L736-L753) —— 先 `ep := AXI_LITE_ENDPOINT_INIT_C` 把端点清零、各 ready 复位，再拷入当前主/从信号，最后调用底层 `axiSlaveWaitTxn` 计算 `writeEnable/readEnable`。

底层写/读等待过程的实现：

[AxiLitePkg.vhd:528-561](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L528-L561) —— `axiSlaveWaitWriteTxn`，注意末尾 `awready := '0'; wready := '0'`，这正是「默认未认领」的来源。

[AxiLitePkg.vhd:563-596](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L563-L596) —— `axiSlaveWaitReadTxn`，结构对称，末尾 `arready := '0'`。

在 `AxiVersion` 里如何调用：

[AxiVersion.vhd:186](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L186) —— `comb` 进程里在 `v := r;` 之后的第一条 AXI 语句就是 `axiSlaveWaitTxn(axilEp, axiWriteMaster, axiReadMaster, v.axiWriteSlave, v.axiReadSlave);`，注意它传入的是「次态变量 `v`」里的从机记录，而不是现态 `r`。

#### 4.1.4 代码实践

**实践目标**：确认 `axilEp` 是 `comb` 进程里的局部 variable、且 `axiSlaveWaitTxn` 传入的是 `v` 而非 `r`。

**操作步骤**：

1. 打开 [AxiVersion.vhd:173-180](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L173-L180)，找到 `comb` 进程的敏感列表与 `variable axilEp : AxiLiteEndpointType;` 声明。
2. 确认 `axilEp` 是 `variable`（不是 `signal`），所以它在 `comb` 进程内「即时」生效，符合组合逻辑语义。
3. 对照 [AxiVersion.vhd:179](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L179) 与 [AxiVersion.vhd:186](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L186)，注意 `v := r;`（拷贝现态）在前，`axiSlaveWaitTxn(..., v.axiWriteSlave, v.axiReadSlave)`（操作次态）紧随其后。

**需要观察的现象**：整个寄存器段对从机记录的所有修改都作用在 `v.*` 上，再由末尾 `rin <= v` 与 `axiReadSlave <= r.axiReadSlave`（现态输出）配合，形成「本拍算次态、下拍才出现在端口」的时序。

**预期结果**：你能向自己解释清楚「为什么 helper 必须传 `v` 而不是 `r`」——因为 helper 在本拍内即时改写次态，若传 `r` 则修改会被下一拍的 `v := r` 覆盖。

#### 4.1.5 小练习与答案

**练习 1**：`AxiLiteEndpointType` 里为什么要把 `axiStatus`（`writeEnable/readEnable`）和四条主/从记录捆在一起？
**答案**：因为后续的 `axiSlaveRegister` / `axiSlaveDefault` 既要读主机地址/数据、又要改从机响应，还要读 `writeEnable/readEnable` 来判断本拍是否需要服务。把它们捆成一个 `inout` 端点变量，调用时只传一个 `ep`，签名干净、不易写错参数顺序。

**练习 2**：`axiSlaveWaitTxn` 在末尾把 `awready/wready/arready` 都清成 `'0'`。这对后面的 `axiSlaveDefault` 有什么意义？
**答案**：它给所有地址设了一个「默认未认领」的基线。后面只有真正匹配的 `axiSlaveRegister` 会通过 `axiSlaveWriteResponse/axiSlaveReadResponse` 把对应 `ready` 拉回 `'1'`。于是 `axiSlaveDefault` 只要检查「`ready` 还是 `'0'`」就能断定这笔访问无人认领（地址未映射），从而回 DECERR。

---

### 4.2 Register / RegisterR 映射：把地址绑到寄存器变量

#### 4.2.1 概念说明

`axiSlaveRegister` 与 `axiSlaveRegisterR` 是端点模式的「地址映射表」。每调用一次，就相当于在表里写一行：

| 调用 | 含义 |
| --- | --- |
| `axiSlaveRegister(ep, addr, offset, v.reg)` | **读/写寄存器**：读时把 `reg` 放上 `rdata`；写时把 `wdata` 装进 `reg`。 |
| `axiSlaveRegisterR(ep, addr, offset, reg)` | **只读寄存器**：读时返回 `reg`；写时**不改动** `reg`（但仍给正常写响应）。 |
| `axiSlaveRegister(ep, addr, v.regArray)` | 把一个 `Slv32Array` 数组铺到连续地址 `addr, addr+4, ...`，常用于 ROM/查找表。 |

「只读」是怎么实现的？关键在一个哨兵值：`axiSlaveRegisterR` 内部以 `constVal = "X"` 调用底层写逻辑，而底层在写分支里判断 `if (constVal /= "X")` 才执行赋值——`"X"` 时跳过赋值，于是写通路被静默关掉，只保留读通路。这是 SURF 用 `std_logic` 的 `'X'` 当「禁写标记」的一个巧妙约定。

地址比较是**字粒度**的：比较时丢掉最低 2 位（`araddr(N-1 downto 2)`），因为 AXI-Lite 一个寄存器占 4 字节（32 位）。`offset` 参数则用来把一个 32 位字里的某几个比特绑到寄存器，支持「一个地址里塞多个小字段」。

#### 4.2.2 核心流程

底层 `axiSlaveRegisterLegacy`（单字、带 constVal）的逻辑：

```
# 读在前，避免同拍读写互相覆盖
if readEnable:
    if std_match(araddr(N-1 downto 2), ADDR(N-1 downto 2)):   # 字粒度+无关位
        rdata(busHi downto offset) := reg(...)                # 把字段读出去
        axiSlaveReadResponse(readSlave)                       # arready<='1'，标记已认领

if writeEnable:
    if std_match(awaddr(N-1 downto 2), ADDR(N-1 downto 2))
       and std_match(wstrb, STROBE_MASK):                     # 字节使能也要匹配
        if constVal /= "X":
            reg(...) := constVal                              # constAssign：写固定值
        else:
            reg(...) := wdata(busHi downto offset)            # 正常写
        axiSlaveWriteResponse(writeSlave)                     # awready/wready<='1'
```

几个要点：

- **读优先**：先处理读再处理写，这样同地址同拍的读写不会让写值覆盖掉本拍该读出的旧值。
- **`std_match` 容许 `'-'`**：`addr` 实参里可以写 `'-'` 做「无关位」，实现「一段地址范围都映射到这个寄存器」。这也是为什么地址窗口大小是 \( 2^{k} \) 字节（`k` 个无关位）。
- **多字寄存器**：当 `reg` 宽于 32 位时，端点版 `axiSlaveRegister` 会用一个 `for` 循环把它拆成若干 32 位字，分别映射到 `addr, addr+4, addr+8, ...` 连续地址。
- **认领即拉 ready**：匹配成功就调用 `axiSlaveReadResponse/axiSlaveWriteResponse`，把对应 `ready` 置 `'1'`，这一拍事务就算被「认领」了。

#### 4.2.3 源码精读

只读机制的核心（`axiSlaveRegisterR` 以 `"X"` 关掉写）：

[AxiLitePkg.vhd:889-901](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L889-L901) —— `axiSlaveRegisterR(ep, addr, offset, reg)` 把 `reg` 拷进临时变量，**仅当 `readEnable='1'`** 时调用 `axiSlaveRegister(ep, addr, offset, regTmp, "X")`，写分支被 `"X"` 跳过。

底层写/读判定与 `"X"` 哨兵：

[AxiLitePkg.vhd:755-800](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L755-L800) —— `axiSlaveRegisterLegacy`。注意第 782 行用 `araddr(ADDR_LEN_C-1 downto 2)` 做字粒度比较；第 791 行 `if (constVal /= "X")` 决定写固定值还是写 `wdata`；第 790 行还要求 `wstrb` 与 `STROBE_MASK` 匹配。

字节使能掩码的生成：

[AxiLitePkg.vhd:1077-1084](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L1077-L1084) —— `genAxiSlaveStrobeMask`：按字段占据的字节范围把 `wstrb` 掩码里对应字节位置 `'1'`，其余为 `'-'`（无关），配合 `std_match` 使用。

读/写响应过程（认领动作）：

[AxiLitePkg.vhd:609-617](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L609-L617) —— `axiSlaveWriteResponse`：把 `awready/wready` 置 `'1'`、记下 `bresp`（`bvalid` 留给 `axiSlaveWaitWriteTxn` 下一拍置位）。

[AxiLitePkg.vhd:619-626](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L619-L626) —— `axiSlaveReadResponse`：记下 `rresp`（`rvalid` 同样留给等待过程）。

`AxiVersion` 的地址映射表（既有读写，也有只读，还有数组）：

[AxiVersion.vhd:188-204](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L188-L204) —— 注意三行典型用法：`x"004"` 是读写 `scratchPad`；`x"008"` 是只读 `upTimeCnt`（`axiSlaveRegisterR`）；`x"400"` 把 `userValues`（一个 `Slv32Array(0 to 63)`）铺成 64 个连续只读字。

数组铺开的实现：

[AxiLitePkg.vhd:944-966](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L944-L966) —— `axiSlaveRegister/axiSlaveRegisterR` 的 `Slv32Array` 重载：循环对每个元素调用单字版本，地址每次 `+4`。

#### 4.2.4 代码实践

**实践目标**：从 `AxiVersion` 的映射表反推出一张「地址 → 寄存器 → 读/写」对照表。

**操作步骤**：

1. 读 [AxiVersion.vhd:188-204](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L188-L204)。
2. 把每行整理成三列：地址（如 `0x000`）、绑定的变量（如 `BUILD_INFO_C.fwVersion`）、访问类型（`R` 表示 `axiSlaveRegisterR` 只读，`RW` 表示 `axiSlaveRegister` 读写）。
3. 对 `x"400"` 这行，结合 [AxiVersion.vhd:198](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L198) 与数组重载的实现，推算它实际占用了哪些地址（`0x400` 起连续 64 个字，即 `0x400..0x4FC`）。

**需要观察的现象**：`offset` 参数在这段里几乎都是 `0`（每个字段独占一个 32 位字）；`axiSlaveRegisterR` 出现的次数远多于 `axiSlaveRegister`，因为版本/时间戳/DNA 这类信息天然只读。

**预期结果**：你得到一张至少 8 行的表，并能指出哪些地址是「软件可写、会产生硬件副作用」的（如 `0x104 fpgaReload`、`0x10C userReset`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `axiSlaveRegisterLegacy` 里「读分支」要写在「写分支」前面？
**答案**：因为读写操作的是同一个变量 `reg`。若先写后读，同地址同拍发生读写时，读出的是「刚写入的新值」而非「本拍应有的旧值」，破坏了「读返回写入前的值」的直觉。先读后写保证读用的是旧值。

**练习 2**：`axiSlaveRegisterR` 是如何保证「软件写一个只读寄存器不会真把它改掉」的？
**答案**：它内部以 `constVal = "X"` 调用 `axiSlaveRegister`，而底层写分支有 `if (constVal /= "X")` 守卫，`"X"` 时直接跳过赋值。所以写访问仍然会得到一个正常的写响应（`axiSlaveWriteResponse` 不被跳过），但 `reg` 内容不变——「写了等于没写，但不报错」。

**练习 3**：地址比较为什么用 `araddr(N-1 downto 2)` 而不是 `araddr(N-1 downto 0)`？
**答案**：AXI-Lite 寄存器按 32 位字编址，一个字占 4 字节，地址最低 2 位是字内字节偏移。寄存器映射以字为单位，所以比较时丢掉低 2 位。这正是「未对齐访问应返回 SLVERR」这条 AXI 规则的另一半——地址解码本身只在字粒度进行。

---

### 4.3 Default 兜底：让未映射地址返回 DECERR

#### 4.3.1 概念说明

`axiSlaveDefault` 是映射表的「收尾」。它在所有 `axiSlaveRegister` 之后调用一次，作用是：如果本拍有一笔事务，但**没有任何一个** `axiSlaveRegister` 认领它（即地址落在映射表之外），就由 `Default` 用指定的响应码（通常是 `AXI_RESP_DECERR_C`）把这笔事务结清，并把端点里改好的从机记录拷回 `v.axiWriteSlave / v.axiReadSlave`。

它的判定依据非常优雅：`axiSlaveWaitTxn` 一开始把所有 `ready` 清零，认领过的 `axiSlaveRegister` 会把对应 `ready` 拉高。于是 `Default` 只需检查「`writeEnable='1'` 且 `awready` 仍是 `'0'`」——这就意味着「有写要处理，但没人认领」。

四种响应码回顾（来自 [u3-l1](u3-l1-axilite-records.md)）：`OK=00`、`EXOKAY=01`（AXI-Lite 不用，占位）、`SLVERR=10`（访问方式非法，如非对齐/用了 WSTRB）、`DECERR=11`（地址无人解码）。SURF 的惯例是：**未映射地址用 `DECERR`**，让软件能立刻发现「访问到了一块不存在的寄存器」。

#### 4.3.2 核心流程

```
axiSlaveDefault(ep, v.axiWriteSlave, v.axiReadSlave, AXI_RESP_DECERR_C):
    if writeEnable='1' and ep.axiWriteSlave.awready='0' and extTxn='0':
        axiSlaveWriteResponse(ep.axiWriteSlave, DECERR)   # 给这笔「野写」一个 DECERR 响应
    if readEnable='1'  and ep.axiReadSlave.arready='0'  and extTxn='0':
        axiSlaveReadResponse(ep.axiReadSlave, DECERR)     # 给这笔「野读」一个 DECERR 响应
    v.axiWriteSlave := ep.axiWriteSlave                   # 把端点里的成果拷回次态变量
    v.axiReadSlave  := ep.axiReadSlave
```

注意最后的「拷回」：前面所有 helper 都在改 `ep` 里的从机记录，只有 `axiSlaveDefault` 把 `ep` 拷回 `v`。这也是为什么 `axiSlaveDefault` 必须是「最后一步」、且**必须调用**——否则你在 `ep` 里做的所有响应修改都丢了。

`extTxn` 参数是一个逃生口：当你想对某个地址做「多拍才能完成」的特殊处理（例如真正去启动一次 I2C 事务再回响应），你会自己接管这笔事务、不让 `Default` 提前结清它，此时置 `extTxn='1'` 告诉 `Default`「别管我」。

#### 4.3.3 源码精读

响应码常量：

[AxiLitePkg.vhd:28-49](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L28-L49) —— `AXI_RESP_OK_C/EXOKAY_C/SLVERR_C/DECERR_C`，以及 `SLVERR`/`DECERR` 各自的触发条件注释。

`axiSlaveDefault`（端点版）实现：

[AxiLitePkg.vhd:1002-1018](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L1002-L1018) —— 第 1009、1013 行的判定条件 `... and awready='0' ...`/`... and arready='0' ...` 就是「无人认领」的检测；末尾两行（1016–1017）把 `ep` 拷回 `axiWriteSlave/axiReadSlave`。

`AxiVersion` 的收尾调用：

[AxiVersion.vhd:207](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L207) —— `axiSlaveDefault(axilEp, v.axiWriteSlave, v.axiReadSlave, AXI_RESP_DECERR_C);`，固定用 `DECERR` 作为未映射响应。

成文规定（AGENTS.md）：

[AGENTS.md:109](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L109) —— 「Use `axiSlaveDefault(...)` at the end of the map so unmapped accesses return the intended response, commonly `AXI_RESP_DECERR_C`.」这正是把上面的惯例写成强制规定的条款。

#### 4.3.4 代码实践

**实践目标**：验证「未映射地址回 DECERR」是 `Default` 主动给出的，而不是寄存器行给的。

**操作步骤**：

1. 读 [AxiVersion.vhd:188-207](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L188-L207)，数一下映射表实际覆盖了哪些地址段（如 `0x000..0x008`、`0x100..0x10C`、`0x300`、`0x400..`、`0x500`、`0x600`、`0x700`、`0x800..`）。
2. 选一个落在所有这些段之外的地址，例如 `0x040` 或 `0xFC0`。
3. 在脑中模拟一次对该地址的写：`axiSlaveWaitTxn` 置 `writeEnable='1'`、`awready='0'`；逐行 `axiSlaveRegister` 都因 `std_match` 不命中而不动 `awready`；最后 `axiSlaveDefault` 看到 `writeEnable='1' and awready='0'`，于是 `axiSlaveWriteResponse(..., DECERR)`。

**需要观察的现象**：没有任何一行 `axiSlaveRegister` 显式写 `DECERR`；`DECERR` 只出现在 `axiSlaveDefault` 的实参里。

**预期结果**：你能清楚说出「未映射地址的 DECERR 完全由 `axiSlaveDefault` 的兜底逻辑提供，寄存器行只负责命中时的 OK 响应」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `axiSlaveDefault` 这一行删掉，会发生什么？
**答案**：`ep` 里被 `axiSlaveWaitTxn/axiSlaveRegister` 改写的从机记录永远不会拷回 `v.axiWriteSlave/v.axiReadSlave`，于是 `rin <= v` 拿不到响应、端口上的从机记录始终停在 `REG_INIT_C`。表现是：所有 AXI-Lite 访问都收不到 `bvalid/rvalid`，总线挂死。所以 `axiSlaveDefault` 即使对「全命中」的表也不能省。

**练习 2**：什么情况下你会给 `axiSlaveDefault` 传 `extTxn='1'`？
**答案**：当你对某个地址实现了「扩展事务」——响应需要多拍才能给出（例如写一个寄存器去启动一次 I2C/SPI 访问，等真正完成再回响应）。此时你已在自己的状态机里置好了 `awready/wready`，不希望 `Default` 在本拍就匆忙给响应，于是传 `extTxn='1'` 让 `Default` 跳过结清。

---

### 4.4 写副作用与跨域状态：两件最易踩坑的事

#### 4.4.1 概念说明

前三个模块讲的是「怎么把地址接进来」。这一模块讲两件 AGENTS.md 反复强调、却容易被新手忽略的事。

**写副作用要显式。** `axiSlaveRegister` 把 `wdata` 写进某个 `v.xxx` 后，这件事的「后果」并不会自动发生——它只是改了一个变量。如果这个寄存器是要「触发某个动作」（启动状态机、清计数器、发脉冲、推进 FIFO），你必须在 `comb` 的次态逻辑里**显式**写出这个因果。例如 `runCmd` 寄存器被写 `1` 后，应该是状态机在 `case` 里看到 `r.runCmd='1'` 而跳出 `IDLE_S`。AGENTS.md 把这条写成「Apply write side effects deliberately. ... should be visible in the surrounding next-state logic」。

**跨域状态要先同步。** AXI-Lite 寄存器块运行在 `axiClk` 域，而它要读的状态（计数器、链路状态、ADC 采样值）常常在另一个时钟域。绝不能把这些异步信号直接塞进 `axiSlaveRegisterR` 让 CPU 读——那是组合跨域采样，会读到亚稳态垃圾。正确做法二选一：（a）把整个寄存器块搬到数据所在的时钟域，用 `AxiLiteAsync` 把 AXI-Lite 总线本身跨域进来；（b）至少用 `Synchronizer` / `SyncStatusVector` / `FifoAsync` 把要读的信号先同步到 `axiClk` 再暴露。AGENTS.md 原文：「synchronize before exposing them on AXI-Lite. Do not read raw signals from another clock domain through a register map.」

#### 4.4.2 核心流程

**写副作用的典型链路**：

```
软件写 0x00 → axiSlaveRegister(ep, 0x00, 0, v.runCmd)   # v.runCmd := wdata(0)
            → 下一拍 r.runCmd = '1'
            → comb 里 case r.state: when IDLE_S => if r.runCmd='1' then v.state := RUNNING_S
            → 状态机开始干活（真正的「副作用」）
```

**跨域同步的典型链路（把总线搬进数据域）**：

```
CPU ──[axilClk 域]── AxiLiteAsync ──[axisClk 域]── 寄存器块
                                      ↑
                          数据/计数器也在 axisClk 域，同域读取，安全
```

`AxiLiteAsync` 内部用异步 FIFO 把 5 个通道安全地搬到目的域（原理见 [u2-l1](u2-l1-cdc-synchronizers.md) 的 CDC 三铁律与 [u2-l2](u2-l2-fifo-blocks.md) 的 Gray 指针异步 FIFO）。这样寄存器块和数据「同域」，`axiSlaveRegisterR` 直接读现态信号就是安全的。

#### 4.4.3 源码精读

写副作用的活例子（`AxiStreamTimer` 的 `runCmd` 触发状态机）：

[AxiStreamTimer.vhd:195](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTimer.vhd#L195) —— `axiSlaveRegister(axilEp, toSlv(0, 11), 0, v.runCmd);`，软件写地址 `0x0` 的 bit0 即改 `runCmd`。

[AxiStreamTimer.vhd:164-170](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTimer.vhd#L164-L170) —— `case r.state is when IDLE_S => if (r.runCmd = '1') then v.state := RUNNING_S; ...`。注意这里的因果：`axiSlaveRegister` 只负责把 `1` 写进变量，真正「启动定时器」这个副作用是状态机在读到 `r.runCmd='1'` 后显式跳转才发生的。两段代码相隔几十行，但它们是一对——这正是「写副作用要在次态逻辑里显式可见」的含义。

`AxiVersion` 里也有同类模式：写 `0x10C`（`userReset`）改 `v.userReset`，而 `userReset` 作为端口输出（[AxiVersion.vhd:251](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L251) `userReset <= r.userReset;`）去复位外部逻辑——「副作用」就是输出端口电平变化。

跨域同步的活例子（`AxiStreamTimer` 用 `AxiLiteAsync`）：

[AxiStreamTimer.vhd:229-245](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTimer.vhd#L229-L245) —— `U_AXIL_CDC : entity surf.AxiLiteAsync`，把外部 `axilClk` 域的总线搬到 `axisClk` 域（`mAxiClk => axisClk`）。于是 `comb` 进程（[AxiStreamTimer.vhd:153-154](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTimer.vhd#L153-L154) 敏感的是 `axilReadIntMaster/axilWriteIntMaster`，已是 `axisClk` 域的同步后信号），而它读的 `r.timer / r.channels(ch).timeSof` 等状态本就产生在 `axisClk` 域——全程同域，不存在亚稳态。`AxiStreamTimer` 暴露的这些时间戳用的是 `axiSlaveRegisterR`（[AxiStreamTimer.vhd:201-202](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTimer.vhd#L201-L202)），因为它们是只读硬件状态。

成文规定：

[AGENTS.md:110-111](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L110-L111) —— 「Apply write side effects deliberately ...」与「For status counters and sampled signals crossing clock domains, synchronize before exposing them on AXI-Lite.」

#### 4.4.4 代码实践

**实践目标**：在一个范例里同时看清「写副作用」和「跨域同步」两件事。

**操作步骤**：

1. 打开 [AxiStreamTimer.vhd:153-219](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTimer.vhd#L153-L219)，这是它的 `comb` 进程。
2. 找到「写副作用」：`axiSlaveRegister(axilEp, toSlv(0,11), 0, v.runCmd)`（195 行）写入后，是状态机 `case r.state` 的 `IDLE_S` 分支（165–170 行）消费它。
3. 找到「跨域同步」：`comb` 用的 `axilReadIntMaster` 来自 [AxiStreamTimer.vhd:229-245](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamTimer.vhd#L229-L245) 的 `AxiLiteAsync` 输出，确认寄存器块运行在 `axisClk`，而 `r.timer/r.channels` 也在 `axisClk`，两者同域。

**需要观察的现象**：`comb` 的敏感列表里没有「原始的、来自其他时钟域」的信号；所有跨域都由 `AxiLiteAsync` 这一个实例统一处理。

**预期结果**：你能指出这个设计若不插 `AxiLiteAsync`（让 `comb` 直接敏感外部 `axilReadMaster`）会违反 AGENTS.md 哪一条，以及可能产生的现象（CPU 偶尔读到错误的 `timer` 值，因为 `timer` 在 `axisClk` 而 AXI 总线在 `axilClk`）。

#### 4.4.5 小练习与答案

**练习 1**：「写 `runCmd` 寄存器」和「定时器开始跑」是同一拍发生的吗？
**答案**：不是。写 `runCmd` 在拍 N 把 `v.runCmd` 改成 `'1'`，经 `seq` 在拍 N+1 才出现在 `r.runCmd`；状态机在拍 N+1 的 `comb` 里看到 `r.runCmd='1'`，算出 `v.state := RUNNING_S`，再经 `seq` 在拍 N+2 才进入 `RUNNING_S`、`timer` 才开始自增。也就是说从软件写寄存器到定时器真正起跑至少相隔 2 拍——这正是双进程「算次态、下拍生效」的固有延迟。

**练习 2**：假如你的状态寄存器来自一个 50 MHz 的外部计数器，而 `axiClk` 是 125 MHz，能不能直接 `axiSlaveRegisterR(ep, addr, 0, extCount)`？
**答案**：不能。`extCount` 在另一个时钟域，直接读是组合跨域采样，存在亚稳态风险，CPU 可能读到跳变中的乱码。应先把计数器用 `SyncStatusVector`（事件计数经 `FifoAsync` 原子跨域）或先整体同步到 `axiClk` 再暴露；或干脆把寄存器块放进 50 MHz 域、用 `AxiLiteAsync` 把总线跨过去（同 `AxiStreamTimer` 做法）。

---

## 5. 综合实践

把本讲四步骨架和两个注意点串起来，写一个最小的 AXI-Lite 从机。**以下为示例代码（不在仓库中），你需要自己新建文件实现它。**

**任务**：实现一个 `TinyAxiLiteSlave`，要求：

- 1 个**读写**寄存器 `scratchPad`（32 位）在地址 `0x00`。
- 1 个**只读**自由计数器 `freeCounter`（32 位）在地址 `0x04`，每个时钟自增。
- 未映射地址（如 `0x08` 及以后）的访问返回 `AXI_RESP_DECERR_C`。
- 沿用双进程骨架与 `TPD_G`、`RST_POLARITY_G`、`RST_ASYNC_G` 三个复位泛型。

**骨架（示例代码）**：

```vhdl
-- 示例代码：本讲综合实践的最小 AXI-Lite 从机
library ieee;
use ieee.std_logic_1164.all;
library surf;
use surf.StdRtlPkg.all;
use surf.AxiLitePkg.all;

entity TinyAxiLiteSlave is
   generic (
      TPD_G          : time := 1 ns;
      RST_POLARITY_G : sl   := '1';
      RST_ASYNC_G    : boolean := false);
   port (
      axiClk         : in  sl;
      axiRst         : in  sl;
      axiReadMaster  : in  AxiLiteReadMasterType;
      axiReadSlave   : out AxiLiteReadSlaveType;
      axiWriteMaster : in  AxiLiteWriteMasterType;
      axiWriteSlave  : out AxiLiteWriteSlaveType);
end entity;

architecture rtl of TinyAxiLiteSlave is
   type RegType is record
      scratchPad    : slv(31 downto 0);
      freeCounter   : slv(31 downto 0);
      axiReadSlave  : AxiLiteReadSlaveType;
      axiWriteSlave : AxiLiteWriteSlaveType;
   end record;
   constant REG_INIT_C : RegType := (
      scratchPad    => (others => '0'),
      freeCounter   => (others => '0'),
      axiReadSlave  => AXI_LITE_READ_SLAVE_INIT_C,
      axiWriteSlave => AXI_LITE_WRITE_SLAVE_INIT_C);
   signal r   : RegType := REG_INIT_C;
   signal rin : RegType;
begin
   comb : process (axiReadMaster, axiWriteMaster, axiRst, r) is
      variable v      : RegType;
      variable axilEp : AxiLiteEndpointType;
   begin
      v := r;

      -- (1) 解码本拍事务，填好端点与 writeEnable/readEnable
      axiSlaveWaitTxn(axilEp, axiWriteMaster, axiReadMaster,
                      v.axiWriteSlave, v.axiReadSlave);

      -- (2) 地址映射表：0x00 读写，0x04 只读
      axiSlaveRegister (axilEp, x"00", 0, v.scratchPad);        -- 读写
      axiSlaveRegisterR(axilEp, x"04", 0, r.freeCounter);       -- 只读：注意传 r（现态）

      -- (3) 兜底：未映射地址回 DECERR，并把 ep 拷回 v
      axiSlaveDefault(axilEp, v.axiWriteSlave, v.axiReadSlave, AXI_RESP_DECERR_C);

      -- 只读计数器的「副作用」：每拍自增（与 AXI 写无关，纯硬件行为）
      v.freeCounter := r.freeCounter + 1;

      -- (4) 同步复位（异步复位在 seq 里处理），与 RST_ASYNC_G 互斥
      if (RST_ASYNC_G = false and axiRst = RST_POLARITY_G) then
         v := REG_INIT_C;
      end if;

      rin <= v;
      -- 输出取现态 r，保证时序
      axiReadSlave  <= r.axiReadSlave;
      axiWriteSlave <= r.axiWriteSlave;
   end process;

   seq : process (axiClk, axiRst) is
   begin
      if (RST_ASYNC_G and axiRst = RST_POLARITY_G) then
         r <= REG_INIT_C after TPD_G;
      elsif rising_edge(axiClk) then
         r <= rin after TPD_G;
      end if;
   end process;
end architecture;
```

**验证要点（可对照 [AxiVersion.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd) 自检）**：

1. **骨架顺序**：`WaitTxn` → `Register`/`RegisterR` → `Default`，与 AGENTS.md 第 107–109 条一致。
2. **只读传现态**：`axiSlaveRegisterR` 传 `r.freeCounter`（现态），因为 `v.freeCounter` 在下面会被自增覆盖，若传 `v` 会读到「本拍 +1 之后」的值。
3. **未映射回 DECERR**：写 `0x08` 应得到 `bresp=11`、读 `0x08` 应得到 `rresp=11` 且 `rdata=0`。
4. **复位分支互斥**：`comb` 里的同步复位与 `seq` 里的异步复位由同一个 `RST_ASYNC_G` 切换，与 [AxiVersion.vhd:239-262](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L239-L262) 完全同构。

**如何跑起来（待本地验证）**：本仓库的回归栈是 cocotb + GHDL（见 [u9-l1](u9-l1-cocotb-toolchain.md)）。可参照 `tests/axi/` 下现有从机测试，写一个最小 testbench：用 `axiLiteBusSimWrite`/`axiLiteBusSimRead`（[AxiLitePkg.vhd:1090-1209](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLitePkg.vhd#L1090-L1209) 提供的仿真过程）对 `0x00` 写后读、对 `0x04` 连续读两次看计数是否递增、对 `0x08` 读写看是否报 `DECODE_ERROR` 警告。具体命令与结果**待本地验证**。

## 6. 本讲小结

- AXI-Lite 从机的标准写法是「四步骨架」：`axiSlaveWaitTxn` 解码事务 → 一串 `axiSlaveRegister`/`axiSlaveRegisterR` 列地址映射表 → `axiSlaveDefault` 兜底，全部写在 `comb` 进程里、操作次态变量 `v`。
- `axiSlaveWaitTxn` 把本拍事务装进端点变量 `axilEp`，用 `writeEnable/readEnable` 标记「本拍要服务」，并把所有 `ready` 清零作为「未认领」基线。
- `axiSlaveRegister` 是读写寄存器，`axiSlaveRegisterR` 是只读寄存器——只读靠底层 `constVal="X"` 跳过写赋值实现；地址按字粒度（丢低 2 位）用 `std_match` 比较，支持 `'-'` 无关位与 `offset` 字段定位。
- `axiSlaveDefault` 靠「`ready` 仍为 0」判断地址未映射，对未映射访问回 `AXI_RESP_DECERR_C`，并负责把 `ep` 拷回 `v`——所以它**必须最后调用且不可省**。
- 写副作用（如 `runCmd` 触发状态机）必须在 `comb` 的次态逻辑里显式表达；跨时钟域的状态必须先用 `AxiLiteAsync`（把总线搬进数据域）或同步器处理，再暴露到 AXI-Lite，绝不直接读异域信号。
- 这套模式由 [AGENTS.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md) 的 *AXI-Lite Register Implementation Pattern* 一节（第 103–113 行）固化为全仓库约定。

## 7. 下一步学习建议

- 读完本讲，你已经能读懂任何一个 SURF 的 AXI-Lite 从机。建议挑一个真实模块做「地址映射表反推」练习：[AxiLiteRegs.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiLiteRegs.vhd) 或 [AxiDualPortRam.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiDualPortRam.vhd)（[u3-l4](u3-l4-axiversion-helpers.md) 会专门讲它们）。
- 想理解「多主多从」如何用地址窗口路由，进入 [u3-l3](u3-l3-axilite-crossbar-master.md)：AXI-Lite 交叉开关、主机事务状态机与异步桥——其中 `AxiLiteAsync` 正是本讲 4.4 提到的跨域桥。
- 想从软件侧对照寄存器映射，跳到 [u9-l4](u9-l4-pyrogue-device-models.md)：用 PyRogue 的 `RemoteVariable` 镜像同一张地址表，体会「改 HDL 寄存器要同步改 PyRogue」这一条 AGENTS.md 规定的实际含义。
