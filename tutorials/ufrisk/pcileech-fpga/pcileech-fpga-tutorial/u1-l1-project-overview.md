# 项目总览：什么是 pcileech-fpga 与 PCIe DMA

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向**完全没接触过 pcileech-fpga** 的读者。读完本讲，你应当能够：

- 用自己的话说清楚 **pcileech-fpga 是什么**、它解决了什么问题；
- 理解 **DMA（直接内存访问）攻击** 的基本概念，以及 **FPGA 硬件** 在其中扮演的角色；
- 搞清楚 pcileech-fpga 与 **PCILeech、MemProcFS、LeechCore** 这三个名字之间的生态关系——谁是硬件、谁是软件、谁连着谁；
- 看懂根目录 `readme.md` 里那张**设备对照表**，知道项目支持哪些设备、用什么方式连接（USB3 / 以太网 / Thunderbolt）、各自的传输速率大概是多少。

本讲只读一个文件：项目根目录的 `readme.md`。我们故意不从任何 `.sv`（SystemVerilog）源码讲起——先把「这是个什么项目」这件事在脑子里建起来，后续讲义再钻进 HDL 代码细节。

---

## 2. 前置知识

本讲几乎不需要任何硬件或 FPGA 背景。下面几个名词会反复出现，先建立直觉即可，不需要死记：

- **PCIe（PCI Express）**：现代电脑里连接显卡、网卡、固态硬盘等高速外设的「总线标准」。它允许设备与主机内存之间高速交换数据。每一代（gen1 / gen2 / gen3）速率不同，物理通道数（x1 / x4 / x16）也不同。
- **DMA（Direct Memory Access，直接内存访问）**：一种「不经过 CPU、由设备自己直接读写主机内存」的机制。这是合法的——你的固态硬盘之所以快，就靠 DMA。但当一个**攻击者控制的设备**插到机器上、未经授权地读写内存时，就构成了 **DMA 攻击**。
- **TLP（Transaction Layer Packet，事务层包）**：PCIe 总线上传输数据的基本「信封」。读写内存、读取配置空间，最终都封装成一个个 TLP 在线上跑。
- **FPGA（现场可编程门阵列）**：一种芯片，里面是大量可重新连线的逻辑单元。你可以用硬件描述语言（HDL，本项目用 SystemVerilog）把 FPGA「编程」成一块定制的 PCIe 设备。
- **HDL（Hardware Description Language，硬件描述语言）**：描述数字电路的语言。本项目大量使用 SystemVerilog（`.sv` 文件）。

> 提示：如果你只想「用」这套工具，理解到这一层就够了；后续要「改」或「移植」，才会需要更深的 PCIe 与 FPGA 知识。本手册会逐步带你走到那一步。

---

## 3. 本讲源码地图

本讲涉及的唯一关键文件：

| 文件 | 作用 |
| --- | --- |
| [readme.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md) | 项目根目录说明文档。它用一张表回答了三个最关键的问题：**支持哪些设备、用什么连接、跑多快**，并说明了项目与 PCILeech / MemProcFS 的关系。 |

> 注意：仓库里还有大量 `.sv`、`.tcl`、`.xdc`、`.coe` 等文件，它们在本讲里**不展开**。本讲只负责给你一张「整体地图」，后续每篇讲义会各取所需地钻进具体文件。

---

## 4. 核心概念与源码讲解

本讲把 `readme.md` 拆成三个最小模块来讲：

1. **DMA 攻击与 FPGA 的角色**——先理解「为什么需要一块 FPGA」。
2. **pcileech-fpga 与 PCILeech / MemProcFS / LeechCore 的生态关系**——再理解「这块板子周围还连着哪些软件」。
3. **支持的设备、连接方式与传输速率**——最后看懂那张设备对照表。

---

### 4.1 DMA 攻击与 FPGA 的角色

#### 4.1.1 概念说明

先抛开项目本身，想一个最朴素的问题：**怎样让一个外设直接读写另一台电脑的内存？**

正常的 PCIe 设备（比如显卡）插在主板上，主板信任它，于是允许它用 DMA 直接访问内存。这意味着——**任何能插进 PCIe 插槽、并且会说 PCIe 协议的设备，理论上都能直接读写目标机器的内存**。这就是 DMA 攻击的物理基础：攻击者带一块「伪装成普通设备」的硬件插到目标机上，绕过操作系统和 CPU，直接把内存里的数据拷走（密码、密钥、进程内存……），或者把数据写回去篡改。

那为什么偏要 **FPGA**？因为普通的 PCIe 设备（网卡、显卡）功能是固定的，不能让你「随便发任意 TLP」。而 FPGA 可以用 HDL 编程成**任意行为**的 PCIe 设备——你可以让它伪装成某个合法设备（厂商 ID、设备 ID 都能改），同时又能发起任意内存读写、收发任意原始 TLP。这种「可定制 + 可协议级操控」正是 FPGA 在 DMA 研究中不可替代的原因。

#### 4.1.2 核心流程

把一次 DMA 内存读取抽象成数据通路，大致是：

```text
攻击者主机(USB/网线/雷电线)
        │  普通命令(读 0x1000 处 4 字节)
        ▼
┌─────────────────────────┐
│  FPGA 板(pcileech-fpga)  │
│  - 通信核心:USB3/以太网   │  ← 接收命令
│  - PCIe 核心:发起 TLP    │  ← 把命令翻译成 PCIe 读请求
└─────────────────────────┘
        │  PCIe 读请求 TLP
        ▼
┌─────────────────────────┐
│   目标主机(被攻击者)      │
│   内存物理地址 0x1000     │  ← 直接被读,不经 CPU/OS
└─────────────────────────┘
        │  返回数据(完成包 CplD)
        ▼
   FPGA 收到数据 → 原路送回攻击者主机
```

关键点有两个：

1. FPGA **同时插在两个方向**上：一端连「攻击者主机」（通过 USB3 / 以太网 / Thunderbolt），另一端插「目标主机」的 PCIe 插槽。
2. FPGA 把攻击者发出的「普通命令」翻译成 **PCIe TLP**，再把目标机返回的数据原路送回。所谓「full access to 64-bit memory space」（64 位内存空间全访问）就是指它能把任意 64 位物理地址作为目标。

#### 4.1.3 源码精读

`readme.md` 开头第一段就是整个项目的「一句话定义」，信息密度很高：

> PCILeech FPGA contains software and HDL code for FPGA based devices that may be used together with the PCILeech Direct Memory Access (DMA) Attack Toolkit and MemProcFS.
> FPGA based hardware provides full access to 64-bit memory space and may also send raw PCIe Transaction Layer Packets TLPs - allowing for more specialized research.

这段对应源码：

- [readme.md:L1-L4](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L1-L4) —— 项目的自我定位。请重点圈出两个关键词：**64-bit memory space**（能访问完整的 64 位物理地址空间）和 **raw PCIe Transaction Layer Packets TLPs**（能收发原始 TLP，而不只是封装好的高层命令）。后者尤其重要：它意味着这块板子不只是「读写内存的工具」，还是「协议级研究平台」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「FPGA 在 DMA 攻击里同时连两个方向」这件事。

**操作步骤**：

1. 重新读一遍本节那张数据通路示意图。
2. 找到 `readme.md` 里描述设备「连接方式（Connection）」那一列（见 [readme.md:L12-L23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L12-L23)），观察有哪些值。

**需要观察的现象**：

- 你会发现「连接方式」这一列描述的是 **FPGA 与攻击者主机之间** 的链路（USB-C / USB3 / Thunderbolt3 / UDP/IP）。
- 而 PCIe 这一列（PCIe Version）描述的才是 **FPGA 与目标主机之间** 的链路。

**预期结果**：能用自己的话说出「一块 FPGA 有两条对外链路：一条给攻击者发命令（USB/网/雷电），一条插进目标的 PCIe 槽」。这一认知会贯穿后续所有讲义。

**待本地验证**：如果你手头没有实物板卡，无法亲眼看到两条链路，这是正常的——本实践是「源码阅读型实践」，重在建立心智模型。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DMA 攻击者要用 FPGA，而不是直接插一块普通网卡？
> **参考答案**：普通 PCIe 设备的功能由厂商固定，只能做它被设计来做的事（网卡只发包收包），不能让你发起任意内存读写或发送任意 TLP。FPGA 可用 HDL 编程成行为完全定制的 PCIe 设备，既能伪装身份（改厂商/设备 ID），又能发起任意 DMA 与原始 TLP，这正是 DMA 研究需要的。

**练习 2**：本节提到 FPGA 能「send raw PCIe Transaction Layer Packets TLPs」，这与「读写内存」有什么区别？
> **参考答案**：「读写内存」是高层操作——你只管给出地址，FPGA 帮你打包成 TLP；而「发送 raw TLP」是底层操作——你可以手工构造任意格式的事务层包，用于协议级研究、特殊探测、绕过某些过滤等更专业的场景。后者更灵活也更危险。

---

### 4.2 pcileech-fpga 与 PCILeech / MemProcFS / LeechCore 的生态关系

#### 4.2.1 概念说明

刚接触的人很容易被这几个相似的名字搞晕：**pcileech-fpga、PCILeech、MemProcFS、LeechCore**，它们到底谁是谁？

其实它们是**同一个生态里的不同层次**，分工明确：

- **pcileech-fpga（本项目）**：**硬件侧**。仓库里是跑在 FPGA 芯片上的 HDL 代码（SystemVerilog）。它本身只是一块「听话的板子」，不会自己决定去读哪个地址。
- **PCILeech**：**命令行工具/攻击工具包**。运行在攻击者的普通电脑上（Windows/Linux），负责下达「读这个地址、写那个地址、dump 这段内存」这类高层命令。
- **MemProcFS**：**内存分析框架**。把 dump 出来的物理内存映射成一个「虚拟文件系统」，让你像浏览文件一样查看进程、模块、注册表等，方便做取证与分析。
- **LeechCore**：**底层采集库**。夹在上述软件和硬件之间，抽象出「从某设备读物理内存」这一通用接口。PCILeech 和 MemProcFS 都通过 LeechCore 去跟真实硬件（或文件、远程源）打交道。

一句话记忆：**PCILeech/MemProcFS 是大脑（下命令、做分析），LeechCore 是神经（抽象采集层），pcileech-fpga 是手脚（真实插在目标机上的硬件）。**

#### 4.2.2 核心流程

把它们串起来的一次典型工作流：

```text
用户在攻击者主机上运行 PCILeech / MemProcFS
        │  "帮我读物理地址 0x1000"
        ▼
   MemProcFS / PCILeech (高层)
        │  转成 LeechCore 采集请求
        ▼
      LeechCore (采集抽象层)
        │  通过 USB3/网/雷电 链路发给板子
        ▼
   pcileech-fpga (FPGA 硬件固件)
        │  把请求翻译成 PCIe 读 TLP
        ▼
   目标主机内存 (被读取) ——数据原路返回
```

注意：**本项目仓库只包含最底层那一段（pcileech-fpga 的 HDL）**。PCILeech、MemProcFS、LeechCore 都是**另外的 GitHub 仓库**，需要单独获取。所以本手册后面讲的几乎全是 HDL/工程文件，不会再展开讲那三个软件的用法——那是另一套文档的事。

#### 4.2.3 源码精读

`readme.md` 第一段就把这套生态点名了：

- [readme.md:L1-L4](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L1-L4) —— 关键句是「may be used together with the PCILeech … Attack Toolkit and MemProcFS」。注意用词是 **together with**（配合使用）：本项目自己不完整，必须和那两个软件一起用。两个外部链接分别指向：
  - `https://github.com/ufrisk/pcileech/`（PCILeech 工具包）
  - `https://github.com/ufrisk/MemProcFS/`（MemProcFS）

  虽然正文没直接出现「LeechCore」这个词，但它是 PCILeech/MemProcFS 与硬件之间的默认采集层，了解它的存在能帮你解释「为什么软件能适配这么多不同型号的板子」。

> 小提示：第 1 讲只要知道「这是配合关系」即可。后续讲义里你会看到 HDL 代码内部有一个「命令/寄存器协议」——那正是 LeechCore 这类宿主软件用来跟板子对话的约定。

#### 4.2.4 代码实践

**实践目标**：把四个名字的「分工」固化成一张能随时查阅的小表。

**操作步骤**：

1. 打开 `readme.md` 第 3 行的两个外部链接，确认它们确实指向另外两个独立仓库（不是本仓库的子目录）。
2. 在自己的笔记里画一张四列小表，分别是：**名字 / 类型（硬件 or 软件）/ 所在位置（本仓库 or 外部）/ 主要职责**，并填入 pcileech-fpga、PCILeech、MemProcFS、LeechCore 四行。

**需要观察的现象**：

- 点击链接会跳到 `github.com/ufrisk/pcileech` 和 `github.com/ufrisk/MemProcFS`，二者都是**独立仓库**，与本仓库（`ufrisk/pcileech-fpga`）平级。

**预期结果**：得到一张清晰说明「谁是硬件、谁是软件、谁在本仓库、谁在外部」的对照表。

**待本地验证**：LeechCore 在 `readme.md` 中并未直接点名，它是否被你的具体工作流调用，取决于你如何安装 PCILeech/MemProcFS。如果你尚未搭建软件环境，只需先记住它的「抽象采集层」定位。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 pcileech-fpga「自己一个人用不起来」？
> **参考答案**：因为本项目只是跑在 FPGA 上的固件（HDL），它需要有人下达「读哪里、写哪里」的命令。下命令、分析结果的是 PCILeech / MemProcFS，它们又通过 LeechCore 跟硬件通信。没有这套宿主软件，板子就只是「一块会 PCIe 的 FPGA」，无法独立完成 DMA 攻击或内存分析。

**练习 2**：如果有人问「我在 PCILeech 命令里看到一段内存读得很慢，是不是 pcileech-fpga 的 HDL 有 bug？」你会如何有理有据地反驳或排查？
> **参考答案**：先分清层次——慢可能来自任何一层：PCIe 链路训练速率、USB/网线的物理带宽、LeechCore 的采集开销、宿主机的处理速度，最后才轮到 HDL。不能一上来就怪 HDL。本讲的目的之一就是让你建立「这是多层生态」的认知，避免过早钻进单一层次。

---

### 4.3 支持的设备、连接方式与传输速率

#### 4.3.1 概念说明

`readme.md` 最实用的部分是两张设备表：**Supported Devices（受支持设备）** 和 **Older / Legacy Devices（旧设备）**。每行描述一块具体的板子。要看懂它，先理解表头每个字段的含义：

| 表头字段 | 含义 |
| --- | --- |
| **Device** | 设备名，也是仓库里的**目录名**（点进去就是该设备的工程）。 |
| **Connection** | FPGA 与**攻击者主机**之间的连接方式（USB3 / USB-C / Thunderbolt3 / UDP/IP / USB2）。 |
| **Transfer Speed** | 大致吞吐速率，单位 MB/s。它往往受 **Connection 这条链路** 限制，而不是 PCIe。 |
| **Version** | 固件版本号（如 4.14、4.15）。 |
| **FPGA** | 实际芯片型号，例如 `XC7A35T-484`（Xilinx Artix-7，型号 35T，484 封装）。 |
| **PCIe Version** | FPGA 与**目标主机**之间的 PCIe 规格（如 gen2 x1、gen2 x4）。 |

> 关键直觉：**两栏速率分别由两条链路决定**——Connection 那一栏限制了「命令/数据进出攻击者主机」的速度，PCIe 那一栏限制了「板子和目标机内存」的速度。最终体验到的速率，取两者中**较慢**的那个。

#### 4.3.2 核心流程

挑选设备时，可以按这个决策树走：

```text
你的核心诉求是什么?
  ├─ 要性价比均衡      → 选 PCIeSquirrel(USB-C, 190MB/s, gen2 x1)  [推荐]
  ├─ 只追求最高速度    → 选 ZDMA(Thunderbolt3, 1000MB/s, gen2 x4)
  ├─ 想用以太网/远程   → 看 NeTV2(UDP/IP, 7MB/s, 旧设备)
  ├─ 手上只有 USB2     → 看 Acorn/FT2232H(USB2, 25MB/s, 旧设备)
  └─ 想要 100T 大容量  → ZDMA / CaptainDMA 100T / CaptainDMA M2 100T
```

特别要注意表下方那条脚注（[readme.md:L25](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L25)）：

> PCILeech FPGA uses PCIe **x1** even if more PCIe lanes are available hardware-wise. This is sufficient to deliver necessary performance.

这句话解释了一个常见疑惑：**为什么很多设备硬件支持 x4，固件却只跑 x1？** 因为对 DMA 攻击而言，瓶颈通常是「板子和攻击者主机之间」那条链路（USB ~190MB/s），PCIe 给到 x1（gen2 x1 约 400MB/s 理论值）已经足够喂饱它，多 lane 反而增加复杂度。这也是为什么你能看到 ZDMA 这种 x4 + Thunderbolt3 的「旗舰」能把速率推到 1000MB/s——它两侧都拉满了。

#### 4.3.3 源码精读

- [readme.md:L8-L23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L8-L23) —— **受支持设备主表**。请重点看几行有代表性的：
  - `ZDMA`：Thunderbolt3，1000 MB/s，`XC7A100T-484`，PCIe gen2 x4 —— **当前最快**。
  - `CaptainDMA M2`：USB-C，190 MB/s，`XC7A35T-325`，gen2 x1-x4 —— **当前 commit 刚加入的设备**（见 git log「CaptainDMA M2 100T」）。
  - `AC701/FT601`：USB3，190 MB/s，`XC7A200T-676`，gen2 x4 —— **体积最大的官方参考板**。
- [readme.md:L25](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L25) —— **「即便硬件有多 lane 也只用 x1」的脚注**。这是理解整个项目速率取舍的钥匙。
- [readme.md:L27](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L27) —— 官方推荐语：性价比优先选 **Screamer PCIe Squirrel**，纯速度优先选 **ZDMA**。
- [readme.md:L52-L64](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L52-L64) —— **旧设备表**。注意其中 `PCIeSquirrel` 一行（即 Screamer PCIe Squirrel）没有写「PCIe Version」，而是直接给了 `XC7A35T-484`——这正是后续讲义的主参考工程。

> 小知识：`XC7A35T` 里的 `A` 表示 Artix-7 系列，`35T` 表示逻辑规模档位（数字越大越强），`-484` 表示 484 引脚的 BGA 封装。后续讲义里的 `*_top.sv`、`.xdc` 约束都和这个具体型号绑定。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把 `readme.md` 的设备信息整理成一张**「设备—FPGA 型号—连接方式—传输速率」对照表**，并用 100 字以内写出项目定位。

**操作步骤**：

1. 打开 [readme.md:L12-L23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L12-L23) 的受支持设备表。
2. 在本地新建一个文本文件（或笔记），按下表表头摘录前 5 个设备：

   | 设备 | FPGA 型号 | 连接方式 | 传输速率 |
   | ---- | --------- | -------- | -------- |
   | ZDMA | XC7A100T-484 | Thunderbolt3 | 1000 MB/s |
   | GBOX | … | … | … |
   | CaptainDMA M2 | … | … | … |
   | CaptainDMA M2 100T | … | … | … |
   | AC701/FT601 | … | … | … |

3. 填完后，把 GBOX 和 ZDMA 对比，回答：**同样用 Thunderbolt3，为什么速率差这么多？**（提示：看 PCIe 那一列与 FPGA 型号差异。）
4. 用不超过 100 字写一段「项目定位」，需包含三个要点：① 是硬件（FPGA HDL）；② 配合 PCILeech/MemProcFS；③ 能访问 64 位内存空间并收发原始 TLP。

**需要观察的现象**：

- 对照表填完后，会发现「连接方式相同、速率不同」的情况（如 GBOX 与 ZDMA 都走 Thunderbolt3），说明速率并非仅由连接方式决定。
- 你会发现 USB-C / USB3 类设备的速率大多落在 **190–220 MB/s** 这一档——这正是 FT601 这类 USB3 桥接芯片的实际能力上限，后续讲义会讲到这里。

**预期结果**：

- 一张完整对照表；
- 一段类似这样的定位说明（示例，仅供参考，请用自己话写）：
  > 「pcileech-fpga 是一套基于 Xilinx Artix-7 的 FPGA HDL 工程，作为 PCILeech/MemProcFS 的硬件端，插在目标主机 PCIe 插槽上，实现对 64 位物理内存空间的直接读写，并能收发原始 PCIe TLP，用于 DMA 攻击与协议级研究。」

**待本地验证**：上表中的速率数字均来自 `readme.md` 的官方声明，实际能否跑满取决于你的主板 PCIe 带宽、USB 控制器、线缆质量等，本实践不要求实测。

#### 4.3.5 小练习与答案

**练习 1**：`CaptainDMA M2`（`XC7A35T-325`）和 `CaptainDMA M2 100T`（`XC7A100T-484`）连接方式都是 USB-C，为什么后者速率更高（190 → 220 MB/s）？
> **参考答案**：速率并非只由「连接方式」一项决定。两者虽同为 USB-C，但 FPGA 型号从 35T（325 封装）升到 100T（484 封装），意味着可用逻辑资源、BRAM 容量、内部数据通路位宽都可能更宽，从而支撑更高的内部吞吐。此外版本号也不同（4.15 vs 4.14）。这说明看设备表要「逐字段」看，不能只盯一列。

**练习 2**：硬件明明支持 PCIe gen2 x4，固件为什么默认只用 x1？请引用 `readme.md` 原文作答。
> **参考答案**：`readme.md:L25` 明确写道：「PCILeech FPGA uses PCIe x1 even if more PCIe lanes are available hardware-wise. This is sufficient to deliver neccessary performance.」——因为对 DMA 攻击而言，瓶颈通常在板子与攻击者主机之间的链路（如 USB ~190MB/s），PCIe x1 已经足够喂饱它，多 lane 只增加复杂度而无收益。

**练习 3**：如果你只能花有限预算买一块板子做学习，官方推荐哪款？如果只追求最快呢？
> **参考答案**：见 [readme.md:L27](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L27)。性价比优先推荐 **Screamer PCIe Squirrel**（对应仓库 `PCIeSquirrel/` 目录，也是本手册后续讲义的主参考工程）；纯速度优先推荐 **ZDMA**。

---

## 5. 综合实践

把本讲三节内容串起来，完成下面这个**贯穿性小任务**：

> **任务：为「向新同事介绍 pcileech-fpga」准备一页 A4 纸的入门说明。**

要求这一页里必须包含：

1. **一句话定位**：参考 4.1.3 和 4.2.3 的关键词（64 位内存空间、原始 TLP、配合 PCILeech/MemProcFS）。
2. **一张生态关系图**：画出 PCILeech / MemProcFS → LeechCore → pcileech-fpga(硬件) → 目标主机内存 的数据流向（参考 4.2.2）。
3. **一张精简设备表**：从受支持表里挑 3 款代表设备（一个最快、一个性价比推荐、一个非 USB 连接的旧设备），列出它们的 FPGA 型号、连接方式、传输速率、PCIe 规格。
4. **一个「易错点提醒」**：写明「速率受两条链路共同限制，且固件常默认用 x1」这个认知，提醒同事不要只看 PCIe 那一栏。

**自检标准**：把这张 A4 给一个完全不懂的人看，他能否在 5 分钟内回答出「这个项目是干嘛的、需要配什么软件、买哪块板子」。如果能，本讲的目标就达成了。

> 说明：本实践是「文档整理型实践」，不涉及运行命令，也不修改任何源码，重点在把零散信息组织成可传递的知识。

---

## 6. 本讲小结

- **pcileech-fpga 是一套基于 Xilinx Artix-7 的 FPGA HDL 工程**，作为 PCIe DMA 硬件端，提供对 64 位物理内存空间的直接访问，并能收发原始 TLP。
- 它**自己不能独立使用**，必须配合 **PCILeech**（攻击工具包）和 **MemProcFS**（内存分析框架）运行，二者常通过 **LeechCore** 这一采集抽象层与硬件通信。
- 设备有**两条对外链路**：一条连攻击者主机（USB3 / USB-C / Thunderbolt / 以太网），一条插目标主机 PCIe 插槽；**最终速率受两条链路中较慢者限制**。
- 即便硬件支持多 lane，固件也**常默认只用 PCIe x1**，因为对 DMA 攻击来说已经够快，瓶颈通常在另一侧链路。
- 受支持设备中，**ZDMA**（Thunderbolt3 + gen2 x4，1000 MB/s）最快；**Screamer PCIe Squirrel**（USB-C，190 MB/s）性价比最佳且是后续讲义的主参考工程。
- `readme.md` 里的设备表，表头字段（Device / Connection / Transfer Speed / Version / FPGA / PCIe Version）是后续挑选、对比、移植设备的钥匙。

---

## 7. 下一步学习建议

本讲让你建立了「这是个什么项目」的整体认知。下一讲 **u1-l2《仓库结构与设备目录组织》** 会带你**真正走进仓库**：

- 看清顶层有哪些设备目录，每个目录内部 `src` / `ip` / `xdc` / `tcl` 的职责划分；
- 识别哪些 HDL 文件是**跨设备复用**的公共文件，哪些是**设备特定**文件；
- 初步区分「较新设备（带 `bar_controller` + `cfgspace_shadow`）」与「较老设备」的源码差异。

建议你现在就做两件事，为下一讲热身：

1. 用文件浏览器随便扫一眼 `PCIeSquirrel/` 目录，看看里面有哪些子目录和文件类型（`.sv` / `.tcl` / `.xdc` / `.coe`）。**只看，不改。**
2. 重读 [readme.md:L27](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L27) 确认为何后续讲义以 `PCIeSquirrel` 为主参考工程。

之后，带着「仓库是怎么组织的」这个问题，进入第 2 讲。
