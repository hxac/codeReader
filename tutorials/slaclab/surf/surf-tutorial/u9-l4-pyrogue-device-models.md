# PyRogue 设备模型

## 1. 本讲目标

学完本讲后，你应当能够：

- 用 `pr.RemoteVariable` 把一个 AXI-Lite 寄存器镜像成软件可读写的变量，并能正确填写 `offset` / `bitSize` / `bitOffset` / `mode` / `base` / `disp` 六个关键字段。
- 区分三类「可触发动作」——`pr.RemoteCommand`（写一个真实寄存器位）、`@self.command`（纯 Python 组合命令）、`pr.LocalCommand`（本地无硬件副作用），并知道何时用哪一种。
- 用 `pr.LinkVariable` 在一个真实变量之上派生出「人类可读」的衍生量，写 `linkedGet` / `linkedSet` / `dependencies`，理解它为何不占硬件地址。
- 看懂 SURF Python 包的导出约定：私有实现文件 `_Xxx.py` + 包 `__init__.py` 里 `from ... import *` 再导出，以及 `setup.py` 如何把它们装成 `surf` 包。
- 对照 RTL 的寄存器偏移表（u3-l4 的 `AxiVersion.vhd`），让一个 PyRogue 设备类与硬件在「名称、偏移、位宽、读写属性」四者上逐字段对齐。

本讲承接 **u3-l4（AxiVersion 与内存映射辅助块）** 和 **u9-l1（cocotb 测试工具链）**：u3-l4 讲清了 RTL 侧的寄存器布局，本讲讲清软件侧如何用 PyRogue 把同一份布局镜像出来；u9-l1 讲清了 RTL 回归仿真，本讲补上「软件如何驱动真实硬件寄存器」这一半。两者最终指向同一条铁律：**RTL 寄存器布局与 PyRogue 镜像必须逐字段同步**。

## 2. 前置知识

在继续前，请确认你已理解下列概念（若陌生，先回看对应讲义）：

- **AXI-Lite 寄存器映射**：地址按字节编址、寄存器按 32 位字对齐，`0x00`/`0x04`/`0x08` 才是连续寄存器（u3-l1、u3-l2）。
- **AxiVersion 的偏移表**：固件版本在 `0x000`、ScratchPad 在 `0x004`、UpTimeCnt 在 `0x008`……git hash 在 `0x600`、构建字符串在 `0x800`（u3-l4）。
- **双进程 RTL 风格** 与 `axiSlaveRegister` / `axiSlaveRegisterR` 的读写绑定（u1-l5、u3-l2）。

三个本讲会用到的补充概念：

- **PyRogue / Rogue 是什么**：Rogue 是 SLAC 的 C++/Python 控制框架，PyRogue 是它的 Python 前端。一条「PyRogue 设备」（`pr.Device`）就是一个 Python 类，它描述「这块硬件有哪些寄存器、各在什么偏移、怎么读写」。运行时 Rogue 通过底层链路（PGP/以太网/内存映射）把这些 Python 变量的读写翻译成真实的 AXI-Lite 总线事务。所以 PyRogue 设备类本身**不碰总线时序**，它只描述「寄存器地图」。
- **RemoteVariable 的「远程」二字**：`Remote` 表示该变量对应一段**远端（FPGA 侧）硬件地址**，每次 `get()`/`set()` 都会产生一次真实的寄存器访问往返；与之相对的是 `LocalVariable`（纯软件本地量，不触发硬件访问）。本讲的主角是前者。
- **base（进制解释器）**：同一个 32 位寄存器，可以当成无符号整数（`pr.UInt`）、当成位串（`pr.Binary`）、甚至当成字符串（`pr.String`，按字节拼出一段文本）。`base` 决定 PyRogue 如何把这 32 位「解释」给用户。

> 术语提示：本讲反复出现「变量（Variable）/ 命令（Command）/ 设备（Device）」。在 PyRogue 里，Device 是一棵树，Variable 和 Command 都是挂在 Device 上的子节点；变量有值可读写，命令无值只能触发。

## 3. 本讲源码地图

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| [python/surf/axi/_AxiVersion.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py) | 本讲主线范例 | `AxiVersion(pr.Device)`：`RemoteVariable`、`RemoteCommand`、`@self.command`、`LinkVariable`、`LocalCommand`、`addRemoteVariables` 的完整集合，是 RTL↔软件对齐的活样本 |
| [python/surf/axi/__init__.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/__init__.py) | 包导出 | 逐行 `from surf.axi._Xxx import *`，把私有实现文件再导出为公开符号 |
| [python/surf/ethernet/udp/__init__.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/ethernet/udp/__init__.py) | LinkVariable 工具函数 | `getPortValue`/`setPortValue`/`getIpValue`/`setIpValue`：端口/IP 的字节序翻转，配合 `LinkVariable` 的 `linkedGet`/`linkedSet` |
| [python/surf/ethernet/udp/_UdpEngineClient.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/ethernet/udp/_UdpEngineClient.py) | LinkVariable 实战 | `ClientRemotePortRaw`（真实寄存器）+ `ClientRemotePort`（LinkVariable 派生）的成对写法 |
| [axi/axi-lite/rtl/AxiVersion.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd) | RTL 对照 | `axiSlaveRegisterR`/`axiSlaveRegister` 的偏移表，是 Python 镜像必须对齐的「真值」 |
| [python/README.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/README.md) | 目录与约定 | 说明 `_Xxx.py` + `__init__.py` 再导出约定，以及「寄存器名称/偏移/位宽/模式须与 RTL 同步」的要求 |
| [setup.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/setup.py) | 打包 | `packages=[...]` 列出 `surf/axi`、`surf/ethernet/udp` 等子包，`package_dir={'':'python'}` 把源码根指向 `python/` |

一句话概括：**`_AxiVersion.py` 是「寄存器地图」，`__init__.py` 是「把地图挂上货架」，`setup.py` 是「打包发货」**。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 RemoteVariable / RemoteCommand**——把真实硬件寄存器与「写一位即触发」的动作镜像成软件节点。
2. **4.2 LinkVariable 与派生变量、本地命令**——在真实变量之上派生「人类可读」量，并区分三种命令。
3. **4.3 包导出约定**——`_Xxx.py` + `__init__.py` + `setup.py` 如何把一堆设备类组装成 `surf` 包。

### 4.1 RemoteVariable 与 RemoteCommand

#### 4.1.1 概念说明

一块 FPGA 的寄存器空间，本质是一张「地址 → 值」的表。PyRogue 的 `pr.RemoteVariable` 就是这张表里的一行：它告诉框架「这个寄存器叫什么名字、在哪个偏移、占几位、只读还是可写、用什么进制解释」。运行时你写 `dev.FpgaVersion.get()`，框架就往 `offset=0x00` 发一次 AXI-Lite 读、把回来的 32 位按 `pr.UInt` 解释成一个整数还给你。

`pr.RemoteCommand` 是 `RemoteVariable` 的「动作版」：它同样绑定一个真实偏移，但语义不是「存一个值」而是「写一下就触发硬件」。典型用法是 FPGA 重载——往重载寄存器写一个 `1`，FPGA 就重新加载镜像。这种寄存器你不关心读回值，只关心「写 1 这个动作」，所以用命令比用变量更贴切。

#### 4.1.2 核心流程

写一个 `pr.Device` 子类的标准流程：

1. 定义 `class Xxx(pr.Device)`，在 `__init__` 里先 `super().__init__(**kwargs)` 把父类建好（这一步搭起 Device 树的骨架）。
2. 对每个真实寄存器，`self.add(pr.RemoteVariable(name=..., offset=..., bitSize=..., mode=..., base=...))`。
3. 对每个「写一位即触发」的动作，`self.add(pr.RemoteCommand(name=..., offset=..., function=lambda cmd: cmd.post(1)))`。
4. （可选）重写 `hardReset` / `initialize` / `countReset` 等钩子，在框架的对应生命周期里插入自定义行为。

`RemoteVariable` 的几个字段含义：

| 字段 | 含义 | 例 |
| --- | --- | --- |
| `offset` | 寄存器字节偏移，必须与 RTL 的 `axiSlaveRegister(..., x"004", ...)` 一致 | `0x00`、`0x04` |
| `bitSize` | 占多少位 | `32`、`160`、`8*256` |
| `bitOffset` | 在该字内的起始位（用于把多个子字段塞进一个 32 位字） | `0x00` |
| `mode` | `'RO'` 只读 / `'RW'` 读写 / `'WO'` 只写 | `'RO'` |
| `base` | 进制/类型解释器 | `pr.UInt`、`pr.String` |
| `disp` | 显示格式串 | `'{:#08x}'`、`'{:d}'` |
| `hidden` | 是否在 GUI 隐藏 | `True` |
| `pollInterval` | 自动轮询周期（秒），>0 才轮询 | `1` |
| `groups` | 分组标签，如 `'NoConfig'` 表示不参与配置保存 | `['NoConfig']` |

#### 4.1.3 源码精读

设备类的骨架与第一个变量——`AxiVersion` 继承 `pr.Device`，构造函数先调父类，再用 `RemoteVariable` 镜像固件版本寄存器：

[python/surf/axi/_AxiVersion.py:25-44](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L25-L44) —— `class AxiVersion(pr.Device)` 与 `FpgaVersion` 变量：`offset=0x00`、`bitSize=32`、`mode='RO'`、`base=pr.UInt`、`disp='{:#08x}'`。

对照 RTL 的同一寄存器，可见偏移与读写属性完全对齐：

[axi/axi-lite/rtl/AxiVersion.vhd:188-189](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L188-L189) —— `axiSlaveRegisterR(axilEp, x"000", 0, BUILD_INFO_C.fwVersion)`：偏移 `0x000`、只读（`RegisterR`），与 Python 侧 `offset=0x00` / `mode='RO'` 一一对应。

可读写寄存器与「写一位即触发」命令对照。`ScratchPad` 是一个可读写的测试寄存器（`mode='RW'`），`FpgaReload` 则是命令——两者偏移相邻（`0x04` 与 `0x104`），但语义不同：

[python/surf/axi/_AxiVersion.py:46-55](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L46-L55) —— `ScratchPad`：`offset=0x04`、`mode='RW'`。

[python/surf/axi/_AxiVersion.py:101-110](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L101-L110) —— `FpgaReload` 是 `pr.RemoteCommand`：`offset=0x104`，`function=lambda cmd: cmd.post(1)` 表示「触发时往该偏移写 1」。注意命令没有 `mode` 字段，因为它不持有值。

对照 RTL，`0x104` 正是 `fpgaReload`，且它是普通 `axiSlaveRegister`（可写），写 1 后由 `comb` 进程把 `v.fpgaReload` 置 1 触发重载：

[axi/axi-lite/rtl/AxiVersion.vhd:192-195](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L192-L195) —— `0x100` haltReload、`0x104` fpgaReload、`0x108` fpgaReloadAddr、`0x10C` userReset 四个连续的可写寄存器。

`RemoteCommand` 的 `function=lambda cmd: cmd.post(1)` 这一行是关键：`cmd.post(value)` 会真正发起一次寄存器写，把 `1` 写到 `offset=0x104`，FPGA 侧 `v.fpgaReload` 随之变 1，触发 `Iprog` 重载。这就是「命令 = 写一位即触发」的实现。

`addRemoteVariables` 用来一次性镜像一批同构寄存器。RTL 里 `userValues` 是一个 `Slv32Array(0 to 63)` 数组、挂在 `0x400` 起、每个 4 字节；Python 侧不必写 64 个 `RemoteVariable`，而用一行展开：

[python/surf/axi/_AxiVersion.py:157-168](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L157-L168) —— `addRemoteVariables(name='UserConstants', offset=0x400, number=numUserConstants, stride=4)`：`number` 是个数、`stride=4` 是相邻元素的字节步长，框架自动生成 `UserConstants[0]`、`UserConstants[1]`……

[axi/axi-lite/rtl/AxiVersion.vhd:198](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L198) —— `axiSlaveRegisterR(axilEp, x"400", userValues)`：RTL 侧把整个数组挂在 `0x400`，Python 侧的 `stride=4` 正好对应每个 32 位元素 4 字节。

#### 4.1.4 代码实践

1. **实践目标**：亲手把一个真实寄存器镜像成 `RemoteVariable`，体会「字段就是寄存器属性」。
2. **操作步骤**：
   - 打开 [python/surf/axi/_AxiVersion.py:35-44](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L35-L44) 与 [axi/axi-lite/rtl/AxiVersion.vhd:188-190](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-lite/rtl/AxiVersion.vhd#L188-L190)。
   - 在纸上（或临时 `.py` 文件里）仿照 `FpgaVersion`，为 `UpTimeCnt` 写出 `RemoteVariable`，要求：`offset=0x08`、`bitSize=32`、`mode='RO'`、`disp='{:d}'`、`units='seconds'`、`pollInterval=1`，然后与文件第 57–69 行对照。
3. **需要观察的现象**：你写的字段集合应与源码逐项吻合；特别注意 `pollInterval=1` 这个字段——它让 GUI 每秒自动读一次这个寄存器，从而让「上电秒数」在界面上动起来。
4. **预期结果**：写出的 `RemoteVariable` 与 [_AxiVersion.py:57-69](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L57-L69) 一致。本实践为纯编写练习，不涉及运行仿真。
5. 若想进一步验证字节对齐，可对照 RTL 第 190 行 `axiSlaveRegisterR(axilEp, x"008", 0, r.upTimeCnt)` 确认偏移 `0x08` 与只读属性一致。

#### 4.1.5 小练习与答案

**练习 1**：`FpgaVersion` 用了 `disp='{:#08x}'`，`UpTimeCnt` 却用 `disp='{:d}'`。为什么不该统一？

**答案**：`disp` 只是「给用户看的显示格式」，与寄存器在硬件里的存储无关。版本号是十六进制更易读（如 `0x00010203` 表示 v1.2.3），而上电秒数是十进制计数更直观。两者底层都是 32 位无符号整数，`disp` 不改变读写，只改变展示。

**练习 2**：把 `FpgaReload` 写成 `pr.RemoteVariable(mode='WO')` 而不是 `pr.RemoteCommand`，功能上能行吗？为什么 SURF 选了命令？

**答案**：功能上勉强可行（写一个 `1` 到 `0x104` 同样触发重载）。但语义上「重载」是一个**动作**而非「存一个值」——你永远不需要读回「上次写了什么」。`RemoteCommand` 在 GUI 上显示成按钮而非输入框，调用方式是 `dev.FpgaReload()` 而非 `dev.FpgaReload.set(1)`，更贴合「按下即触发」的心智模型。

### 4.2 LinkVariable 与派生变量、本地命令

#### 4.2.1 概念说明

`LinkVariable` 不对应任何硬件地址，它**派生自一个或多个真实变量**。典型场景是把「对机器友好的原始值」翻译成「对人友好的展示值」。例如 `UpTimeCnt` 是个秒数整数，但用户想看 `1 day, 3:42:05` 这种时长；又例如远端端口寄存器按 big-Endian 存储，但配置时想用人类习惯的 little-Endian 写入。这类「读出后加工 / 写入前翻转」就靠 `LinkVariable` 的 `linkedGet` / `linkedSet` 回调完成。

命令也有三种，分清它们是本模块的重点：

- **`pr.RemoteCommand`**：绑定真实偏移，`function` 里 `cmd.post(value)` 真正写硬件（如 `FpgaReload`）。
- **`@self.command`（装饰器命令）**：纯 Python 方法，**没有偏移**，在方法体里组合调用其它变量/命令（如先设地址再触发重载）。
- **`pr.LocalCommand`**：本地命令，`function` 是个普通 Python 函数，**完全不碰硬件**，只在本机跑（如打印状态）。

#### 4.2.2 核心流程

派生一个 `LinkVariable`：

1. 先有一个真实变量（`RemoteVariable`），它提供原始值。
2. 写一个 `linkedGet(var, read)` 回调：从 `var.dependencies[0].get(read=read)` 拿到原始值，加工后返回。
3. （可写派生量还要写）`linkedSet(var, value, write)` 回调：把人类输入 `value` 翻译回原始值，再 `var.dependencies[0].set(..., write=write)`。
4. `self.add(pr.LinkVariable(name=..., variable=self.Xxx, linkedGet=..., linkedSet=...))`，或在可写情况下用 `dependencies=[...]` 指明依赖。

三种命令的选用口诀：**要写硬件位 → RemoteCommand；要编排多步 → @self.command；只在本机算 → LocalCommand**。

#### 4.2.3 源码精读

`UpTime` 派生自 `UpTimeCnt`，把秒数格式化成时长。注意它通过 `variable=self.UpTimeCnt` 绑定依赖、`linkedGet=parseUpTime` 做翻译：

[python/surf/axi/_AxiVersion.py:71-87](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L71-L87) —— `parseUpTime` 先读 `dependencies[0]`（即 `UpTimeCnt`），若为全 1（无效）返回 `'Invalid'`，否则用 `datetime.timedelta` 转成 `HH:MM:SS` 字符串；`UpTime` 这个 `LinkVariable` 只读、`disp='{}'`。

`GitHashShort` 用 `dependencies=[self.GitHash]` 的写法（与 `variable=` 等价的另一种依赖声明），并用 lambda 内联做位移截取：

[python/surf/axi/_AxiVersion.py:192-198](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L192-L198) —— 从 160 位 git hash 里右移 132 位、取低 7 位十六进制，得到短 hash；全 0 时判定为「dirty（未提交代码）」。

`BuildStamp` 是 `base=pr.String` 的好例子——256 字节当成一段文本读，再用 `parse.parse(...)` 拆出 `ImageName`/`BuildEnv`/`BuildServer`/`BuildDate`/`Builder` 五个 `LinkVariable`：

[python/surf/axi/_AxiVersion.py:210-243](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L210-L243) —— `BuildStamp` 用 `bitSize=8*256`、`base=pr.String`；`parseBuildStamp` 用 `parse.parse(...)` 模板匹配构建字符串，按 `var.name` 返回对应字段。

可写 `LinkVariable` 的经典场景是字节序翻转。UDP 客户端的远端端口寄存器按 big-Endian 配置，但用户想用 little-Endian 输入。`_UdpEngineClient.py` 先放一个隐藏的真实变量，再放一个可读写的派生量：

[python/surf/ethernet/udp/_UdpEngineClient.py:22-38](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/ethernet/udp/_UdpEngineClient.py#L22-L38) —— `ClientRemotePortRaw`（真实 16 位寄存器，`hidden=True`）+ `ClientRemotePort`（`LinkVariable`，`linkedGet=udp.getPortValue`、`linkedSet=udp.setPortValue`、`dependencies=[...]`）。

`getPortValue`/`setPortValue` 的翻转逻辑放在包的 `__init__.py` 里供复用：

[python/surf/ethernet/udp/__init__.py:17-24](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/ethernet/udp/__init__.py#L17-L24) —— 把 2 字节在 big/little 两种字节序间互转，让用户用顺手的端面、寄存器存硬件要求的端面。

三种命令的对照集中在一个文件里。`FpgaReload` 是 `RemoteCommand`（前面已见）；`FpgaReloadAtAddress` 是 `@self.command`——它**没有 offset**，只是先设地址寄存器再调重载命令；`UserRst` 同理，先写 1 再写 0：

[python/surf/axi/_AxiVersion.py:124-144](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L124-L144) —— `@self.command(hidden=True)` 装饰的 `FpgaReloadAtAddress(arg)` 与 `@self.command(description='Toggle UserReset')` 装饰的 `UserRst()`，方法体里组合调用既有变量/命令。

`PrintStatus` 是 `LocalCommand`，`function=self.getStatus`，纯本地拼字符串打印，**完全不访问硬件地址**（它内部虽然读了若干寄存器，但那是通过 `getStatus` 调用真实变量，命令本身没有 offset）：

[python/surf/axi/_AxiVersion.py:273-277](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiVersion.py#L273-L277) —— `pr.LocalCommand(name='PrintStatus', function=self.getStatus, hidden=True)`。

#### 4.2.4 代码实践

1. **实践目标**：阅读一段字节序翻转的 `LinkVariable`，画出「用户值 → 翻转 → 寄存器值」的数据流。
2. **操作步骤**：
   - 打开 [python/surf/ethernet/udp/_UdpEngineClient.py:22-38](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/ethernet/udp/_UdpEngineClient.py#L22-L38) 与 [python/surf/ethernet/udp/__init__.py:17-24](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/ethernet/udp/__init__.py#L17-L24)。
   - 假设用户要把客户端远端端口设为 `0x1234`（按人类 little-Endian 写）。手算 `setPortValue` 把它翻成什么值写入 `ClientRemotePortRaw`。
3. **需要观察的现象**：`setPortValue` 先 `to_bytes(2,'little')` 再 `from_bytes(...,'big')`，等价于把两字节的高低位互换。
4. **预期结果**：`0x1234` → 字节序互换 → `0x3412` 写入真实寄存器。即 `ClientRemotePort.set(0x1234)` 最终让 `ClientRemotePortRaw` 存下 `0x3412`。这正是 u6-l3 讲过的「端口/IP 按 big-Endian 配置」在软件侧的落地。
5. 本实践为源码阅读 + 手算，不运行命令；如需验证可自行写一个 `import` 了 `surf.ethernet.udp` 的小脚本调用 `setPortValue` 打印结果（依赖 pyrogue 环境，**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`UpTime` 和 `GitHashShort` 一个用 `variable=self.UpTimeCnt`、一个用 `dependencies=[self.GitHash]`，两者有什么区别？

**答案**：没有本质区别，都是声明「这个派生量依赖哪个真实变量」。`variable=` 是单依赖的简写，`dependencies=[...]` 支持多依赖（派生量可同时读多个真实变量）。`GitHashShort` 用列表写法只是风格统一；若一个派生量要同时依赖 `GitHash` 和别的变量，就必须用 `dependencies`。

**练习 2**：若要把 `FpgaReloadAtAddress` 改成 `pr.RemoteCommand`，能行吗？

**答案**：不行。`RemoteCommand` 只能往**一个**固定偏移写一个值，而 `FpgaReloadAtAddress` 需要「先写地址寄存器（`0x108`）、再触发重载（`0x104`）」两步、且地址是运行时参数 `arg`。这种「编排多步、带参数」的逻辑只能用 `@self.command` 这种纯 Python 方法实现，方法体里组合调用 `self.FpgaReloadAddress.set(arg)` 和 `self.FpgaReload()`。

### 4.3 包导出约定

#### 4.3.1 概念说明

SURF 的 Python 代码不把每个设备类直接写成公开文件，而是遵循一条严格的「私有实现 + 公开再导出」约定：每个设备类写在带下划线前缀的 `_Xxx.py` 里（Python 里下划线开头表示「内部」），再由所在包的 `__init__.py` 用 `from ._Xxx import *` 把它重新导出为公开符号。这样用户写 `from surf.axi import AxiVersion` 就能拿到类，而实现细节藏在私有文件里。`setup.py` 最后把这些子包列进 `packages=[...]`、用 `package_dir={'':'python'}` 告诉打包工具「源码根是 `python/` 而非仓库根」，从而装出一个叫 `surf` 的包。

#### 4.3.2 核心流程

从「写一个设备类」到「用户能 import」的链路：

1. 实现：在 `python/surf/<subsys>/_Xxx.py` 里写 `class Xxx(pr.Device)`。下划线前缀表示它是私有实现。
2. 再导出：在 `python/surf/<subsys>/__init__.py` 里加一行 `from surf.<subsys>._Xxx import *`，把 `Xxx` 变成包的公开符号。
3. 打包：在 [setup.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/setup.py) 的 `packages=[...]` 里登记 `surf/<subsys>`，确保 `pip install` 时这个子包被装进去。
4. 版本：`setup.py` 在安装时用 `git describe --tags` 推版本号，追加写进顶层 `python/surf/__init__.py` 的 `__version__`。

`__init__.py` 里 `import *` 之所以安全，是因为每个 `_Xxx.py` 都通过定义 `class Xxx` 暴露了明确的名字；`*` 把这些类名收进包命名空间。新增一个设备类时，**改完 `_Xxx.py` 必须同步在 `__init__.py` 加一行再导出**，否则用户 import 不到——这条与 u1-l1 提到的「改 HDL 须同步最近的 `ruckus.tcl`」是同一种「实现与清单同步」的约定。

#### 4.3.3 源码精读

`axi` 包的 `__init__.py` 是一张「私有文件 → 公开符号」的清单，`_AxiVersion` 在第 18 行被导出：

[python/surf/axi/__init__.py:10-28](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/__init__.py#L10-L28) —— 逐行 `from surf.axi._Xxx import *`，其中第 18 行 `from surf.axi._AxiVersion import *` 把 `AxiVersion` 类暴露为 `surf.axi.AxiVersion`。

`udp` 子包的 `__init__.py` 不仅再导出三个设备类，还**在包级别定义了 `getPortValue` 等工具函数**，供 `_UdpEngineClient.py` 通过 `udp.getPortValue` 调用——这是「工具函数放包级、设备类引用包名」的复用模式：

[python/surf/ethernet/udp/__init__.py:11-13](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/ethernet/udp/__init__.py#L11-L13) —— `from surf.ethernet.udp._UdpEngineClient import *` 等三行再导出；其后的 `getPortValue`/`setPortValue` 等是包级函数。注意 `_UdpEngineClient.py` 里 `from surf.ethernet import udp` 后用 `udp.getPortValue` 引用它们，二者形成循环友好的「包 ↔ 成员」引用。

`python/README.md` 把这条约定钉成文档要求：

[python/README.md:14](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/README.md#L14) —— 「Implementation modules usually use private filenames such as `_AxiVersion.py` and are re-exported from package `__init__.py` files. Keep register names, offsets, bit offsets, modes, and descriptions synchronized with the corresponding RTL packages」。

`setup.py` 负责把这一切装成包。`packages=[...]` 列出了 `surf`、`surf/axi`、`surf/ethernet/udp` 等所有子包；`package_dir={'':'python'}` 把源码根指向 `python/`；版本号来自 `git describe --tags` 并追加进顶层 `__init__.py`：

[setup.py:22-55](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/setup.py#L22-L55) —— `setup(name='surf', version=pyVer, packages=['surf','surf/axi',...,'surf/ethernet/udp',...], package_dir={'':'python'})`。

[setup.py:9-20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/setup.py#L9-L20) —— `rawVer = repo.git.describe('--tags')` 推版本号，并在第 19–20 行把 `__version__="..."` 追加写到 `python/surf/__init__.py`（这就是为什么仓库里的顶层 `__init__.py` 平时只有版本检查、`__version__` 在安装时才出现）。

顶层 `python/surf/__init__.py` 本体非常短，主要做最低版本守卫：

[python/surf/__init__.py:10-11](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/__init__.py#L10-L11) —— `rogue.Version.minVersion('6.1.0')`，确保运行时 Rogue 框架版本不低于 6.1.0。

#### 4.3.4 代码实践

1. **实践目标**：走通「实现 → 再导出 → 打包」三步，理解为什么缺一不可。
2. **操作步骤**：
   - 假设你要新增一个设备类 `Foo`。先确认实现文件命名应为 `python/surf/axi/_Foo.py`（下划线前缀）。
   - 打开 [python/surf/axi/__init__.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/__init__.py)，确定你需要在其中追加的一行是 `from surf.axi._Foo import *`，并按字母序找到插入位置（在 `_AxiStreamTimer` 之后）。
   - 打开 [setup.py:25-53](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/setup.py#L25-L53)，确认 `surf/axi` 已在 `packages` 列表里（无需新增子包则不必改 setup.py）。
3. **需要观察的现象**：若只写 `_Foo.py` 而忘了在 `__init__.py` 加再导出行，则 `from surf.axi import Foo` 会报 `ImportError`，但 `from surf.axi._Foo import Foo` 仍可用——这正是「私有文件 vs 公开符号」的边界。
4. **预期结果**：三步齐备后，`from surf.axi import Foo` 成功。本实践为源码阅读与「找位置」练习，不实际新增文件、不运行打包（实际 `pip install` 需 pyrogue/rogue 环境，**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么设备类要放在 `_Xxx.py` 而非 `Xxx.py`，再用 `__init__.py` 再导出？

**答案**：下划线前缀是 Python 的「内部实现」约定，提示用户「不要直接从 `_Xxx` 导入」。集中由 `__init__.py` 再导出，能统一控制公开 API 表面——哪天要重命名或拆分实现文件，只需改 `__init__.py` 一处，用户侧 `from surf.axi import AxiVersion` 不受影响。这与 RTL 侧用 `*Pkg.vhd` 集中暴露接口、把细节藏在包体内的思路一致。

**练习 2**：`setup.py` 里 `package_dir={'':'python'}` 是什么意思？

**答案**：它告诉 setuptools「空串（根包）对应的源码目录是 `python/`」。于是 `surf` 包的真实文件在 `python/surf/`、而非仓库根的 `surf/`。没有这一句，打包工具会在仓库根找 `surf/` 目录而找不到。

## 5. 综合实践

**任务**：对照 `AxiVersion.vhd` 的寄存器偏移表，从零写一个最小的 PyRogue 设备类 `MiniAxiVersion`，镜像其中**两个**寄存器，并保证名称、偏移、位宽、读写属性四者与 RTL 完全一致。然后把综合实践升级为「三件套」：再加一个只读派生量与一个组合命令。

参考答案（示例代码，非项目原有文件）：

```python
# 示例代码：python/surf/axi/_MiniAxiVersion.py （本讲为练习自造，非仓库既有文件）
import pyrogue as pr

class MiniAxiVersion(pr.Device):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # 1) 镜像 FpgaVersion：RTL axiSlaveRegisterR(x"000",0,fwVersion) -> 只读、0x00、32 位
        self.add(pr.RemoteVariable(
            name='FpgaVersion',
            description='FPGA Firmware Version Number',
            offset=0x00,
            bitSize=32,
            mode='RO',
            base=pr.UInt,
            disp='{:#08x}',
        ))

        # 2) 镜像 ScratchPad：RTL axiSlaveRegister(x"004",0,scratchPad) -> 可读写、0x04、32 位
        self.add(pr.RemoteVariable(
            name='ScratchPad',
            description='Register to test reads and writes',
            offset=0x04,
            bitSize=32,
            mode='RW',
            base=pr.UInt,
            disp='{:#08x}',
        ))

        # 3) 升级：只读派生量，把版本号高字节单独取出展示（LinkVariable 不占硬件地址）
        self.add(pr.LinkVariable(
            name='FpgaMajor',
            mode='RO',
            dependencies=[self.FpgaVersion],
            linkedGet=lambda read: (self.FpgaVersion.get(read=read) >> 24) & 0xFF,
        ))

        # 4) 升级：组合命令，把 ScratchPad 写 0xDEADBEEF 再读回（@self.command，无 offset）
        @self.command(description='Stamp ScratchPad with a magic value')
        def StampMagic():
            self.ScratchPad.set(0xDEADBEEF)
            return self.ScratchPad.get()
```

完成后的自检清单：

- [ ] `FpgaVersion` 的 `offset=0x00`、`mode='RO'` 与 RTL 第 188 行 `axiSlaveRegisterR(axilEp, x"000", ...)` 一致。
- [ ] `ScratchPad` 的 `offset=0x04`、`mode='RW'` 与 RTL 第 189 行 `axiSlaveRegister(axilEp, x"004", ...)` 一致。
- [ ] `FpgaMajor` 是 `LinkVariable`，没有 `offset`（派生量不占地址）。
- [ ] `StampMagic` 用 `@self.command` 装饰，无 `offset`（组合命令）。
- [ ] 若要让它能被 `from surf.axi import MiniAxiVersion` 导入，还须在 [python/surf/axi/__init__.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/__init__.py) 加一行 `from surf.axi._MiniAxiVersion import *`（练习中不要真的改仓库文件）。

这个练习把本讲三个最小模块串起来：`RemoteVariable` 镜像真实寄存器、`LinkVariable` 派生展示量、`@self.command` 编排动作，并落实「RTL↔PyRogue 四字段对齐」这条贯穿全讲的铁律。

## 6. 本讲小结

- `pr.RemoteVariable` 用 `offset`/`bitSize`/`bitOffset`/`mode`/`base`/`disp` 六个字段把一个真实 AXI-Lite 寄存器镜像成软件变量；这些字段必须与 RTL 的 `axiSlaveRegister(R)` 偏移与读写属性逐一对齐。
- `pr.RemoteCommand` 绑定真实偏移、靠 `function=lambda cmd: cmd.post(1)` 实现「写一位即触发」，适合 FPGA 重载这类动作；它没有 `mode`，因为不持有值。
- `pr.LinkVariable` 不占硬件地址，靠 `linkedGet`/`linkedSet`/`dependencies`(或 `variable=`) 在真实变量之上派生「人类可读」量，典型用途是秒数→时长、字节序翻转、字符串拆分。
- 三种命令要分清：`RemoteCommand` 写硬件位、`@self.command` 编排多步纯 Python、`LocalCommand` 本地无硬件副作用。
- `addRemoteVariables` 用 `number`+`stride` 一次性镜像一批同构寄存器，对应 RTL 的数组映射（如 `userValues` @ `0x400`）。
- 包导出约定是「私有实现 `_Xxx.py` + `__init__.py` 里 `from ... import *` 再导出 + `setup.py` 的 `packages`/`package_dir` 打包」三件套；改实现须同步改 `__init__.py`，与 RTL 侧「改 HDL 须改 `ruckus.tcl`」同理。

## 7. 下一步学习建议

- **u9-l5（器件、收发器、Xilinx 家族封装与贡献流程）**：把本讲的「设备类对齐」推广到 `python/surf/devices/` 下的厂商器件模型，看一个器件核如何让 RTL（`devices/`）、构建清单（`ruckus.tcl`）与 PyRogue 镜像（`python/surf/devices/`）三者保持一致。
- **回头精读 u3-l4**：现在你已经会读 Python 镜像，可以更主动地用本讲的偏移表去反向校验 `AxiVersion.vhd` 的 `axiSlaveRegister` 调用，体会「RTL 是真值、PyRogue 是镜像」的双向核对。
- **扩展阅读**：浏览 [python/surf/axi/_AxiStreamMonAxiL.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/axi/_AxiStreamMonAxiL.py) 与 [python/surf/protocols/rssi/](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/protocols/) 下的设备类，观察更复杂的 `LinkVariable`、`groups`、`pollInterval` 用法，以及它们如何与 u4-l4、u7-l2 的 RTL 监控/状态寄存器对齐。
- **如需运行验证**：安装 pyrogue/rogue（≥6.1.0）后，仿照 `setup.py` 的 `pip install` 流程装好 `surf` 包，再写一个小脚本 `from surf.axi import AxiVersion` 实例化、连上 Rogue 链路，亲手 `get()`/`set()` 几个寄存器，把本讲的「字段即寄存器」从纸面落到真实总线事务上（依赖硬件或 Rogue 仿真桥，**待本地验证**）。
