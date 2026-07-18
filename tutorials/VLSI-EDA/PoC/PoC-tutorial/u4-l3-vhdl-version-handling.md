# VHDL 版本处理：v93 与 v08

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 PoC 为什么要在同一个源码树里维护「同一个包的两份实现」，并理解 `.v93.vhdl` / `.v08.vhdl` 文件名后缀编码了什么信息。
- 解释 VHDL「受保护类型（protected type）」是什么、它在哪个语言版本里才被标准化，以及为什么 VHDL-93 的实现必须换一种完全不同的写法。
- 读懂 `common.files` 这份 pyIPCMI 编译清单里的 `if (VHDLVersion ...)` 条件分支，能追踪出「给定一个 VHDL 版本，最终到底编译了哪几个物理文件」。
- 把「编译期选择文件」和 u3-l2 讲过的「展开期 generate 选择实体」这两种可移植机制区分开来，理解它们各自发生在流程的哪一阶段。

本讲只讲「版本」这一个维度。厂商（Vendor）维度已在 u3-l2 讲过，板级配置已在 u2-l3 讲过，仿真辅助包本身已在 u4-l1 讲过——本讲会复用这些结论，但不重复展开。

## 2. 前置知识

在进入源码前，先用最通俗的方式建立三个概念。

### 2.1 VHDL 是一个「有版本」的语言

VHDL 是一门被国际标准化的硬件描述语言（IEEE 1076），它不像 Python 那样只有一个「当前版本」，而是同时存在多个仍在使用的语言标准，最常见的有：

| 标准年份 | 常见简称 | 与本讲相关的能力 |
| --- | --- | --- |
| IEEE 1076-1987 | VHDL-87 | 早期版本，基本不再用 |
| IEEE 1076-1993 | VHDL-93 | **没有**受保护类型；`shared variable` 可以是普通类型 |
| IEEE 1076-2002 | VHDL-02 | 引入受保护类型；`shared variable` 必须是受保护类型 |
| IEEE 1076-2008 | VHDL-08 | 受保护类型得到完善，并加入大量语法糖 |

不同 EDA 工具（仿真器、综合器）支持的版本不同。比如老一些的 ModelSim 默认按 93 版编译，而较新的 Vivado/Vivado 仿真器可以按 08 版编译。一份 VHDL 源码如果用了 08 版才有的语法，拿到只支持 93 的工具上就会直接报语法错误。

### 2.2 「受保护类型」要解决什么问题

仿真里经常需要一块「多个进程都能读写」的全局状态，比如一个全局的「仿真是否已停止」标志。VHDL 用 `shared variable`（共享变量）来表达这种跨进程状态。但「多个进程同时写一个变量」会有竞争——读到的可能是半更新的脏值。

受保护类型（protected type）就是用来解决这个问题的：它把状态藏在类型内部，只暴露一组「方法（过程/函数）」来访问，并且语言保证每次方法调用是原子的（不会被另一个进程的调用打断）。可以把它近似理解成其它语言里的「线程安全对象」或「 monitors」。

- VHDL-93：**没有**受保护类型。只能用「全局 `shared variable` + 一堆自由过程」来勉强模拟，正确性靠使用者自律。
- VHDL-2002 起：受保护类型被标准化，`shared variable` 必须是受保护类型。

这就是本讲的核心张力：PoC 的某些包**必须**维护两份实现——一份给 93（用老办法），一份给 02/08（用受保护类型）。

### 2.3 `.files` 不是 VHDL，是 pyIPCMI 的「菜谱」

回顾 u2-l1：`.files` 文件**不是 VHDL 源码**，而是 pyIPCMI 基础设施读取的编译清单。它用几条简单指令（`vhdl`、`include`、`if`）告诉 pyIPCMI「按什么顺序、在什么条件下、把哪个物理文件编译进 `PoC` 库」。

这意味着「选哪份源码」这件事，可以发生在**编译之前**——pyIPCMI 读 `.files` 时就已经决定好要不要把 `fileio.v93.vhdl` 编进去。这和 VHDL 里的 `generate`（u3-l2）是两个不同阶段的选择机制，本讲第 4.3 节会专门对比。

> 名词提示：本讲会反复出现「逻辑包（logical package）」和「物理文件（physical file）」两个词。`PoC.FileIO` 是一个逻辑包名，而 `fileio.v93.vhdl` 和 `fileio.v08.vhdl` 是实现它的两个物理文件——同一时刻只能有一个被编译进库。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/common/` 和 `src/sim/` 下：

| 文件 | 角色 |
| --- | --- |
| [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files) | 公共包的编译清单，包含按版本选择 fileio 的 `if` 分支（本讲核心）。 |
| [src/common/fileio.v93.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v93.vhdl) | `FileIO` 包的 **VHDL-93** 实现：全局 `shared variable` + 自由过程。 |
| [src/common/fileio.v08.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl) | `FileIO` 包的 **VHDL-02/08** 实现：受保护类型 `T_LOGFILE` 等。 |
| [src/common/protected.v08.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/protected.v08.vhdl) | `ProtectedTypes` 包，提供 `P_BOOLEAN`/`P_INTEGER` 等受保护类型，只在 02/08 分支编译。 |
| [src/sim/sim.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim.files) | 仿真辅助包的编译清单，包含**与 common.files 完全同构**的版本分支，是另一组绝佳范例。 |
| [src/common/common.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.vhdl) | 定义 `context Common`，里面有一句 `use PoC.FileIO.all;`，说明 fileio 是公共套餐的一部分。 |

辅助参考（不展开细读，只点一下出处）：`src/common/my_project.vhdl.template` 里的 `MY_OPERATING_SYSTEM` 常量被两份 fileio 共同使用，用来决定换行符风格。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **版本后缀约定**——文件名如何编码 VHDL 版本。
2. **受保护类型**——同一份功能在 v93 与 v08 下的两种写法。
3. **条件编译**——`.files` 如何在编译前按版本挑文件。

### 4.1 版本后缀约定

#### 4.1.1 概念说明

PoC 想做到「一份源码树，跨多种 VHDL 标准都能用」。当某个包**不得不用**新版本才有的语法（最典型的就是受保护类型）时，PoC 的做法不是在源码里写一堆 `-- synthesis translate_off` 之类的条件注释，而是**把两份互斥的实现各自存成一个物理文件，用文件名后缀标注它对应的语言版本**：

```
<name>.v93.vhdl   →  只能用 VHDL-93 语法
<name>.v08.vhdl   →  用了 VHDL-2002/2008 才有的语法
```

这两份文件对外声明的是**同一个逻辑包名**（比如都写 `package FileIO is`），所以它们是「二选一」的关系：在任意一次编译里，pyIPCMI 只会把其中**一个**送进编译器，绝不会同时编译，否则会因为「同名包重复声明」而冲突。

> 为什么后缀叫 `v08` 却涵盖了 2002 和 2008？因为受保护类型在 2002 就已经可用，2008 只是进一步完善。PoC 用 `v93` 表示「无受保护类型的老世界」，用 `v08` 笼统表示「可以用受保护类型的新世界」（2002 和 2008 都算）。第 4.3 节会看到 `.files` 里实际的阈值是 `< 2002`，恰好对应这个划分。

#### 4.1.2 核心流程

要识别一个版本相关的包，按下面的步骤走：

1. 在仓库里搜所有形如 `*.v93.vhdl` 与 `*.v08.vhdl` 的文件。
2. 把后缀去掉 `.v<NN>.vhdl` 之后，比较剩下的「逻辑名」是否相同——相同就说明它们是一对「同一逻辑包的两份版本实现」。
3. 翻到这两个文件里看 `package XXX is` 那一行，确认它们声明的包名一致（这点保证了它们可以互换）。

```
逻辑名 = 去掉 .v93.vhdl / .v08.vhdl 之后的文件主干
        ├─ .v93.vhdl  →  93 分支专用
        └─ .v08.vhdl  →  02/08 分支专用（二者只编译其一）
```

#### 4.1.3 源码精读

在仓库里搜索，一共能找到 5 对（其中 4 对在 `src/sim/`、1 对在 `src/common/`）外加一个 `protected.v08`：

```
src/common/fileio.v93.vhdl      ↔  src/common/fileio.v08.vhdl
src/sim/sim_random.v93.vhdl     ↔  src/sim/sim_random.v08.vhdl
src/sim/sim_global.v93.vhdl     ↔  src/sim/sim_global.v08.vhdl
src/sim/sim_simulation.v93.vhdl ↔  src/sim/sim_simulation.v08.vhdl
src/sim/sim_unprotected.v93.vhdl ↔ src/sim/sim_protected.v08.vhdl   ← 注意这一对名字不同
src/common/protected.v08.vhdl   （只有 08 版，没有 93 对偶）
```

绝大多数成对文件的逻辑名完全相同。唯独仿真状态机那一对故意取了不同的名字：v93 版叫 `sim_unprotected`（未受保护），v08 版叫 `sim_protected`（受保护）——名字本身就把「能不能用受保护类型」这件事喊了出来，是理解整个机制最直观的入口（u4-l1 已从这个角度讲过仿真包，本讲只借它印证命名约定）。

以本讲主角 fileio 为例，两份文件的包声明分别如下。

VHDL-93 版（包声明在 [src/common/fileio.v93.vhdl:48](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v93.vhdl#L48)）：

```vhdl
package FileIO is
    constant C_LINEBREAK : string;
    ...
end package;
```

VHDL-08 版（包声明在 [src/common/fileio.v08.vhdl:40](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl#L40)）：

```vhdl
package FileIO is
    subtype T_LOGFILE_OPEN_KIND is FILE_OPEN_KIND range WRITE_MODE to APPEND_MODE;
    constant C_LINEBREAK : string;
    ...
end package;
```

两份文件的第 1 行包名都是 `package FileIO is`，这就是「同一个逻辑包的两份实现」的铁证。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不需要运行仿真器）。

1. **实践目标**：用文件名后缀约定，自己盘点出 PoC 里所有「同一逻辑包的两份版本实现」。
2. **操作步骤**：
   - 在仓库根目录执行 `find src -name '*.v93.vhdl'` 与 `find src -name '*.v08.vhdl'`（或用编辑器的全局文件搜索）。
   - 把结果按「去掉版本后缀后的逻辑名」配对。
   - 对每一对，分别打开两个文件，确认它们的 `package XXX is` 行声明的是同一个包名。
3. **需要观察的现象**：每一对文件的包名一致；`protected.v08.vhdl` 找不到 v93 对偶。
4. **预期结果**：得到上面 4.1.3 节列出的那张表，并能解释「同名包 ⇒ 互斥编译」这一推论。
5. 若你不方便运行 `find`，可改为在 GitHub 网页上用 `t` 键快速浏览 `src/common/` 与 `src/sim/` 目录，肉眼挑出带 `.v93.` / `.v08.` 中缀的文件。

#### 4.1.5 小练习与答案

**练习 1**：假如有人不小心把 `fileio.v93.vhdl` 和 `fileio.v08.vhdl` 同时编译进了 `PoC` 库，会发生什么？为什么？

> **答案**：会报「同一个库下 `FileIO` 包被重复声明」的错误。因为两份文件声明的都是 `package FileIO is`，VHDL 不允许一个库里有两个同名包。这正是它们必须「二选一」的根本原因。

**练习 2**：`sim_unprotected.v93.vhdl` 与 `sim_protected.v08.vhdl` 这一对的逻辑名并不相同，这是否破坏了「同名 ⇒ 互斥」的规则？

> **答案**：没有破坏。互斥的本质是「不能同时编译进同一个库」，而不是「文件名必须一致」。这两个文件在 `sim.files` 里被放在同一个 `if/elseif` 的两个分支里（见 4.3 节），逻辑上仍然只编译其中一个；只是 PoC 故意用不同名字强调它们采用了不同的语言设施。

---

### 4.2 受保护类型：v93 与 v08 的两种写法

#### 4.2.1 概念说明

这是本讲最核心的一节。我们来看「同一份功能」在两个语言版本下到底长什么样。

受保护类型的语法骨架（仅 2002/2008 可用）是：

```vhdl
-- 包声明里：只写「接口」
type T_LOGFILE is protected
    procedure OpenFile(...);
    impure function IsOpen return boolean;
end protected;

-- 包体里：写「实现」，状态藏在内部 variable 里
type T_LOGFILE is protected body
    variable Local_IsOpen : boolean;     -- 内部状态，外部看不到
    procedure OpenFile(...) is ... end;
    impure function IsOpen return boolean is
    begin return Local_IsOpen; end;
end protected body;
```

它的关键性质有三：

1. **封装**：内部 `variable` 对外不可见，只能通过声明的方法访问。
2. **原子性**：语言保证同一时刻只有一个方法调用在执行，从而安全地用于 `shared variable`。
3. **`impure function`**：凡是会读取内部状态的方法，必须声明成 `impure function`（「不纯」函数，因为有副作用/依赖隐藏状态）。纯 `function` 不允许读受保护类型的内部状态。

VHDL-93 没有这套机制。要实现「一块被多个进程共享的可变状态」，只能：

- 在包体里声明**模块级**的 `file` 与 `shared variable`（普通类型，不是受保护类型）；
- 写一堆**自由过程（free procedure）**直接读写这些全局量；
- 用 `impure function` 读取它们（因为读了 `shared variable`，同样算副作用）。

这种写法天生是「单例（singleton）」——全局只有一份状态；而且没有原子性保证，并发安全要靠使用者自己保证。

#### 4.2.2 核心流程

下面用伪代码对比两份 fileio 的内部结构。两者的**对外目的相同**（提供一个全局日志文件 + 标准输出的打印接口），但**内部组织截然不同**：

```
┌─────────────── fileio.v93（老世界）────────────────┐
│  包体内放「全局单例状态」：                          │
│    file          LogFile_FileHandle : TEXT;         │
│    shared var    LogFile_State_IsOpen : boolean;    │
│    shared var    LogFile_LineBuffer  : LINE;        │
│  + 一组自由过程直接操作它们：                        │
│    procedure LogFile_Open(...)                      │
│    procedure LogFile_Print(str)                     │
│    impure function LogFile_IsOpen return boolean    │
│  → 只可能存在「一个」全局日志文件                    │
└─────────────────────────────────────────────────────┘

┌─────────────── fileio.v08（新世界）────────────────┐
│  状态被封装进受保护类型：                            │
│    type T_LOGFILE is protected body                 │
│       variable Local_IsOpen  : boolean;             │
│       variable Local_FileName: string(1 to 256);    │
│       procedure OpenFile(...)                        │
│       impure function IsOpen return boolean         │
│    end protected body;                              │
│  + 同样有 T_FILE、T_STDOUT 两个受保护类型           │
│  → 每个变量是一份独立实例，可同时开多个文件          │
└─────────────────────────────────────────────────────┘
```

一句话总结：**v93 = 全局变量 + 自由过程（单例）；v08 = 受保护类型 + 方法（可实例化）**。

#### 4.2.3 源码精读

先看 v93 版。它的全部状态直接挂在包体顶层，位于 [src/common/fileio.v93.vhdl:78-80](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v93.vhdl#L78-L80)：

```vhdl
file            LogFile_FileHandle   : TEXT;
shared variable  LogFile_State_IsOpen : boolean := FALSE;
shared variable  LogFile_LineBuffer   : LINE;
```

注意第二个声明：`shared variable ... : boolean`——这是一个**普通 boolean 类型**的共享变量。这在 VHDL-93 里合法，但在 2002 之后是**非法**的（2002 起要求 `shared variable` 必须是受保护类型）。这恰好反过来说明了为什么这份文件只能走 93 分支。

围绕这些全局量，v93 提供了一组自由过程。读取状态的查询被写成 `impure function`，见 [src/common/fileio.v93.vhdl:96-99](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v93.vhdl#L96-L99)：

```vhdl
impure function LogFile_IsOpen return boolean is
begin
    return LogFile_State_IsOpen;
end function;
```

它必须 `impure`，因为它读取了 `shared variable`（隐藏的外部状态）。

再看 v08 版。它先把一个受保护类型的**接口**声明在包说明里，见 [src/common/fileio.v08.vhdl:47-58](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl#L47-L58)：

```vhdl
type T_LOGFILE is protected
    procedure  OpenFile(FileName : string; OpenKind : T_LOGFILE_OPEN_KIND := WRITE_MODE);
    impure function OpenFile(...) return FILE_OPEN_STATUS;
    procedure  OpenFile(Status : out FILE_OPEN_STATUS; ...);
    impure function IsOpen return boolean;
    procedure  CloseFile;
    procedure  Print(str : string);
    procedure  PrintLine(str : string := "");
    procedure  Flush;
end protected;
```

注意三点：方法都「挂」在 `T_LOGFILE` 上（不再是无主的自由过程）；返回内部状态的 `IsOpen` 被标成 `impure function`；接口里完全看不到内部变量。

对应的实现写在包体的 `protected body` 里，入口在 [src/common/fileio.v08.vhdl:93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl#L93)：

```vhdl
type T_LOGFILE is protected body
    variable LineBuffer     : LINE;
    variable Local_IsOpen   : boolean;
    variable Local_FileName : string(1 to 256);
    ...
    impure function IsOpen return boolean is
    begin
        return Local_IsOpen;
    end function;
    ...
end protected body;
```

这里 `Local_IsOpen` 是受保护类型**内部**的 `variable`，外部代码无法直接访问，只能通过 `IsOpen` 方法读——这就是封装。

v08 版的 fileio 还通过 `use` 引入了受保护类型工具包，见 [src/common/fileio.v08.vhdl:37](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl#L37)：

```vhdl
use PoC.ProtectedTypes.all;
```

这个 `ProtectedTypes` 包就来自 `protected.v08.vhdl`。它提供了一整套「受保护版基础类型」，位于 [src/common/protected.v08.vhdl:42-47](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/protected.v08.vhdl#L42-L47)（以 `P_BOOLEAN` 为例）：

```vhdl
type P_BOOLEAN is protected
    procedure        Clear;
    procedure        Set(Value : boolean := TRUE);
    impure function  Get return boolean;
    impure function  Toggle return boolean;
end protected;
```

其实现把一个 boolean 藏在内部变量里，并提供线程安全的 `Set`/`Get`/`Toggle`，见 [src/common/protected.v08.vhdl:106-129](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/protected.v08.vhdl#L106-L129)：

```vhdl
type P_BOOLEAN is protected body
    variable InnerValue : boolean := FALSE;
    impure function Get return boolean is
    begin
        return InnerValue;
    end function;
    impure function Toggle return boolean is
    begin
        InnerValue := not InnerValue;
        return InnerValue;
    end function;
end protected body;
```

同理还有 `P_INTEGER`、`P_NATURAL`、`P_POSITIVE`、`P_REAL`（见 [src/common/protected.v08.vhdl:52-99](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/protected.v08.vhdl#L52-L99)）。它们的用途就是给那些「需要跨进程共享、又想要原子访问」的场景提供合法的 `shared variable` 类型——这也是 u4-l1 讲过的仿真全局状态机所依赖的基础设施。

> 两份 fileio 还共享一个无关版本的小细节：常量 `C_LINEBREAK` 在包说明里只声明不赋值（[fileio.v93.vhdl:50](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v93.vhdl#L50) / [fileio.v08.vhdl:44](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl#L44)），真正的值写在包体里——这是 VHDL 的「延迟常量（deferred constant）」，因为它要依赖 `ite`/`str_equal` 这些函数才能算出来（按 `MY_OPERATING_SYSTEM` 决定用 `CRLF` 还是 `LF`）。这个细节和版本无关，提一下是为了避免你误以为它也是版本差异。

#### 4.2.4 代码实践

这是一个**对比阅读型实践**。

1. **实践目标**：亲手比对「查询日志文件是否打开」这个功能，在两个版本下分别是如何实现的，并把差异填进下表。
2. **操作步骤**：
   - 打开 [src/common/fileio.v93.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v93.vhdl)，找到 `impure function LogFile_IsOpen`（约 96 行）和它读取的 `shared variable LogFile_State_IsOpen`（约 79 行）。
   - 打开 [src/common/fileio.v08.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl)，找到受保护类型 `T_LOGFILE` 的 `impure function IsOpen`（约 124 行）和它读取的内部 `variable Local_IsOpen`（约 95 行）。
   - 按下表逐项填写：

   | 维度 | v93 (`LogFile_IsOpen`) | v08 (`T_LOGFILE.IsOpen`) |
   | --- | --- | --- |
   | 被读取的状态量放在哪 | 包体顶层 `shared variable` | 受保护类型内部 `variable` |
   | 能否同时存在多份日志 | 否（全局单例） | 是（每个实例独立） |
   | 访问是否受语言原子性保护 | 否 | 是 |
   | 调用语法 | `LogFile_IsOpen`（自由函数） | `SomeLogFileVar.IsOpen`（方法调用） |

3. **需要观察的现象**：v93 版的状态量是「无主的」全局量，任何过程都能直接读写；v08 版的状态量被锁在 `protected body` 里，外部只能通过方法碰。
4. **预期结果**：你会清楚看到「受保护类型 = 状态 + 方法 + 封装 + 原子性」这四件事，而 v93 版只做到了「状态 + 过程」，缺封装与原子性。
5. 本实践为纯阅读，不涉及运行；若想运行验证，可在支持 08 的仿真器里写一个最小 testbench 实例化 `T_LOGFILE`，观察方法调用语法（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 v08 版 fileio 里的 `IsOpen` 必须写成 `impure function`，而不能写成普通 `function`？

> **答案**：因为它读取了受保护类型内部的状态变量 `Local_IsOpen`。VHDL 规定，凡是读取受保护类型内部状态（或任何隐藏/共享状态）的函数都必须声明为 `impure`；纯 `function` 不允许有这种依赖隐藏状态的副作用。

**练习 2**：如果有人把 `fileio.v93.vhdl` 拿到一个**只支持 VHDL-2002** 的工具上编译，会怎样？

> **答案**：会因为 `shared variable LogFile_State_IsOpen : boolean;` 这一句报错——2002 起，`shared variable` 的类型必须是受保护类型，普通 `boolean` 不再合法。这正是该文件只能走「VHDLVersion < 2002」分支的深层原因。

**练习 3**：`protected.v08.vhdl` 里的 `P_BOOLEAN` 和 v93 里的普通 `boolean` 共享变量，都能当 `shared variable` 用吗？区别是什么？

> **答案**：在 VHDL-93 里，两者都可以（语言不限制）；但在 2002/2008 里，只有受保护类型（如 `P_BOOLEAN`）才能作 `shared variable` 的类型。区别在于 `P_BOOLEAN` 提供了受语言保护的原子 `Set`/`Get`/`Toggle` 方法，而裸 `boolean` 共享变量没有原子性保证、且在新版本里根本不合法。

---

### 4.3 条件编译：`.files` 如何按版本挑文件

#### 4.3.1 概念说明

前面两节解释了「为什么要有两份文件」。这一节回答「到底由谁来挑、在什么时候挑」。

挑文件的角色是 **pyIPCMI**（u1-l3、u5-l1 会深入），挑的依据是 `.files` 清单里的 `if` 指令。关键在于**时机**：

- pyIPCMI 在**真正调用编译器之前**就读完 `.files`，决定好要把哪些物理文件送进编译器、以什么顺序送。
- 也就是说，`fileio.v93.vhdl` 和 `fileio.v08.vhdl` 里**只有一份**会被送到编译器眼前，另一份压根不参与编译。这就彻底避免了「新语法在老编译器上报错」的问题——老编译器根本看不见那份含新语法的文件。

这一点要和 u3-l2 的厂商选择机制**严格区分**：

| 维度 | `.files` 版本/厂商选择（本讲） | VHDL `generate` 选择（u3-l2） |
| --- | --- | --- |
| 发生阶段 | **编译前**（pyIPCMI 选文件） | **展开期 elaboration**（VHDL 语义阶段） |
| 谁来做 | 仓库外的 Python 基础设施 | VHDL 工具自身 |
| 粒度 | 选「整个物理文件」 | 选「架构内的某段硬件」 |
| 典型判据 | `VHDLVersion` / `ToolChain` / `Environment` | `DEV_INFO.Vendor` 枚举 |

两者都服务于「一份源码、多处可移植」，但层次不同。`sync_Bits`（u3-l2）用 `generate` 在**同一个文件里**按厂商选实体；fileio 用 `.files` 在**编译前**按版本选文件。两者还可以叠加——比如某个厂商专用文件本身也带 `.v08` 后缀。

#### 4.3.2 核心流程

`common.files` 里和版本相关的逻辑是一个**嵌套的 `if`**，外层按工具链守门，内层按版本三分。伪代码如下：

```
if 工具链不是 Altera_QuartusII 也不是 Lattice_Diamond:   ← 外层守门
    if VHDLVersion < 2002:                                ← 老世界
        编译 fileio.v93.vhdl
    elseif VHDLVersion <= 2008:                           ← 新世界（02 或 08）
        先编译 protected.v08.vhdl
        再编译 fileio.v08.vhdl                            ← 注意先后顺序
    else:                                                 ← 太新也不行
        report "VHDL version not supported."
```

三个要点：

1. **外层守门**：Altera Quartus II 与 Lattice Diamond 的工具链**完全跳过 fileio**（不编译任何一份）。这与 u2-l1 讲过的「用 ToolChain 排除 Altera/Lattice 的 fileio」一致——那两家自带仿真器的文件 IO 行为有坑，干脆不编这份实验性包。
2. **三分阈值**：阈值取在 `< 2002`，恰好就是受保护类型被标准化的年份。`>= 2002 且 <= 2008` 走 08 分支；大于 2008（例如某个想象中的更新标准）直接报「不支持」。注意它**不是** `< 2008`——这是受保护类型 2002 起可用这一事实的直接体现。
3. **编译顺序**：08 分支里 `protected.v08.vhdl` 必须排在 `fileio.v08.vhdl` **之前**，因为后者有 `use PoC.ProtectedTypes.all;`，依赖前者先编译进库。`.files` 的列表顺序就是编译顺序。

#### 4.3.3 源码精读

完整版本分支位于 [src/common/common.files:19-28](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L19-L28)：

```vhdl
if (ToolChain not in ["Altera_QuartusII", "Lattice_Diamond"]) then
    if (VHDLVersion < 2002) then
        vhdl  poc  "src/common/fileio.v93.vhdl"
    elseif (VHDLVersion <= 2008) then
        vhdl  poc  "src/common/protected.v08.vhdl"
        vhdl  poc  "src/common/fileio.v08.vhdl"
    else
        report "VHDL version not supported."
    end if
end if
```

逐行解读：

- 第 19 行：外层守门，Altera/Lattice 直接整个跳过。
- 第 20 行：`VHDLVersion < 2002`（即 93）→ 只编译 v93 版 fileio，**完全不碰** `protected.v08`。
- 第 22 行：`<= 2008`（即 2002 或 2008）→ 先 `protected.v08` 再 `fileio.v08`。
- 第 25–26 行：比 2008 还新的版本，直接 `report` 报错，不编任何 fileio。

再看 [src/common/common.files:30-32](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L30-L32) 的另一处条件：

```vhdl
if (Environment = "Simulation") then
    include "src/sim/sim.files"
end if
```

这是第三种判据 `Environment`——只在仿真环境里才把整个 `sim.files` 拉进来。可见 `.files` 的条件指令可以按 `VHDLVersion`、`ToolChain`、`Environment` 三个独立维度组合。

`sim.files` 是和 `common.files` **完全同构**的另一份范例，位于 [src/sim/sim.files:8-24](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim.files#L8-L24)：

```vhdl
if (ToolChain != "Cocotb") then
    vhdl  poc  "src/sim/sim_types.vhdl"
    if (VHDLVersion < 2002) then
        vhdl  poc  "src/sim/sim_random.v93.vhdl"
        vhdl  poc  "src/sim/sim_global.v93.vhdl"
        vhdl  poc  "src/sim/sim_unprotected.v93.vhdl"
        vhdl  poc  "src/sim/sim_simulation.v93.vhdl"
    elseif (VHDLVersion <= 2008) then
        vhdl  poc  "src/sim/sim_random.v08.vhdl"
        vhdl  poc  "src/sim/sim_protected.v08.vhdl"
        vhdl  poc  "src/sim/sim_global.v08.vhdl"
        vhdl  poc  "src/sim/sim_simulation.v08.vhdl"
    else
        report "VHDL version not supported."
    end if
    vhdl  poc  "src/sim/sim_waveform.vhdl"
end if
```

它用同样的 `< 2002` / `<= 2008` 三分法，把仿真辅助包也分成两套（u4-l1 已详述其内容）。注意 v93 分支里的 `sim_unprotected.v93` 与 v08 分支里的 `sim_protected.v08`——这正是 4.1.3 节提到的那对「名字不同」的文件，被同一个 `if/elseif` 安排成了互斥关系。版本无关的 `sim_types` 与 `sim_waveform` 不带后缀，两个分支都会编译。

最后补一个关联细节：`context Common`（[src/common/common.vhdl:31-41](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.vhdl#L31-L41)）里有一句 `use PoC.FileIO.all;`（[第 35 行](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.vhdl#L35)）。它说明 fileio 被算作公共套餐的一部分——但要注意，由于 fileio 在 Altera/Lattice 上**根本不会被编译**，这条 `use` 在那两家工具链上理论上会找不到包；这是 PoC 已知的「实验性」折中，也呼应了 fileio 文档头里那句「Not yet recommended for adoption」。

#### 4.3.4 代码实践

这是规格里指定的本讲核心实践。

1. **实践目标**：在 `common.files` 中定位依据 `VHDLVersion` 选择 fileio 的分支，并解释 `protected.v08` 为什么只在 2008 分支里编译。
2. **操作步骤**：
   - 打开 [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files)，找到第 19–28 行的嵌套 `if`。
   - 分别假设两个场景，在脑海里「执行」一遍这份清单：
     - **场景 A**：`VHDLVersion = 1993`、工具链是 GHDL。 traced 出编译了哪些文件。
     - **场景 B**：`VHDLVersion = 2008`、工具链是 GHDL。 traced 出编译了哪些文件、先后顺序如何。
   - 对比两个场景下 `protected.v08.vhdl` 的命运。
3. **需要观察的现象**：
   - 场景 A 走 `if (VHDLVersion < 2002)` 分支，只编译 `fileio.v93.vhdl`，`protected.v08.vhdl` **不在列表里**。
   - 场景 B 走 `elseif (VHDLVersion <= 2008)` 分支，依次编译 `protected.v08.vhdl` → `fileio.v08.vhdl`。
4. **预期结果**：你能回答「为什么 `protected.v08` 只在 2008 版编译」——给出**两层原因**：
   - **语言层面**：`protected.v08.vhdl` 通篇是 `type ... is protected ... end protected;` / `... is protected body` 语法，这是 VHDL-2002 才标准化的。把它交给一个 93 版的编译器，会直接语法报错。
   - **清单层面**：`common.files` 把它放在 `elseif (VHDLVersion <= 2008)` 分支内、且 `if (VHDLVersion < 2002)` 分支里**根本没有它**。所以当版本 < 2002 时，pyIPCMI 压根不会把这份文件送给编译器——93 编译器永远看不见它，自然不会报错。
   - 换句话说：「语言不允许 93 用」+「清单保证 93 看不见它」共同实现了安全的不编译。
5. 进阶验证（**可选，待本地验证**）：如果本地装了 GHDL，可分别用 `--std=93` 与 `--std=08` 编译 `PoC` 库，观察两次分别实际分析（analyze）了哪些 fileio 文件。这需要先按 u1-l3 配好 pyIPCMI。

#### 4.3.5 小练习与答案

**练习 1**：把 `common.files` 第 23、24 行的顺序对调（先 `fileio.v08` 再 `protected.v08`），会发生什么？

> **答案**：编译会失败。因为 `fileio.v08.vhdl` 第 37 行写了 `use PoC.ProtectedTypes.all;`，它要求 `ProtectedTypes` 包（即 `protected.v08.vhdl`）已经先编译进 `PoC` 库。顺序一颠倒，`fileio.v08` 编译时就找不到它依赖的包。这体现了 `.files` 列表顺序 = 编译顺序 = 依赖拓扑序。

**练习 2**：如果未来 VHDL-2019 被广泛使用，按当前 `common.files` 的写法，fileio 会怎样？

> **答案**：会命中 `else` 分支，执行 `report "VHDL version not supported."`，不编译任何 fileio 文件。要支持 2019，需要把条件改成 `elseif (VHDLVersion <= 2019)`（前提是这两份实现都兼容 2019）。

**练习 3**：`.files` 的版本选择（编译前挑文件）和 `sync_Bits` 里的 `generate` 厂商选择（展开期挑实体），能不能用在同一个问题上？各有什么取舍？

> **答案**：可以解决同一类「可移植」问题，但层次不同。`.files` 选文件的优点是「不兼容的语法在编译前就被排除」，缺点是「两份实现维护两份文件、容易漂移」。`generate` 选实体的优点是「逻辑集中在一个文件、便于对照」，缺点是「要求所有分支语法都被当前编译器接受」（不能用 08 语法写一个分支、再用 93 工具编译）。PoC 的规则是：**版本差异**（语法不兼容）用 `.files` 选文件；**厂商差异**（语法兼容、只是实现不同）用 `generate` 选实体。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「全链路追踪」任务。

**任务背景**：假设你要向同事解释「在 VHDL-93 与 VHDL-2008 下，PoC 的 `FileIO` 包到底有什么不同」，请产出一张完整的对比说明书。

**操作步骤**：

1. **确定编译了哪些文件**：阅读 [common.files:19-28](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L19-L28)，分别列出 93 与 08 两个版本下参与编译的物理文件清单（注意 08 多了一个 `protected.v08`）。
2. **确定对外 API 的差异**：对比 [fileio.v93.vhdl:48-71](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v93.vhdl#L48-L71) 与 [fileio.v08.vhdl:40-79](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl#L40-L79)，指出 v93 暴露的是自由过程（`LogFile_Open`、`LogFile_Print`、`StdOut_Print`…），而 v08 暴露的是受保护类型（`T_LOGFILE`、`T_FILE`、`T_STDOUT`）。
3. **确定内部实现的差异**：对比 [fileio.v93.vhdl:78-80](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v93.vhdl#L78-L80) 的全局 `shared variable` 与 [fileio.v08.vhdl:93-163](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl#L93-L163) 的 `protected body`，指出单例 vs 实例化、无封装 vs 封装、无原子性 vs 原子性。
4. **解释为什么不冲突**：用 4.1 的「同名包互斥」+ 4.3 的「编译前挑文件」两条结论，说明为什么两份文件能安全共存于同一仓库而从不打架。
5. **画一张时序图**：画出从「pyIPCMI 读 `.files`」到「编译器看到某一份 fileio」的全过程，标注「另一份 fileio 在哪一步被排除」。

**预期成果**：一张包含「文件清单 / API 形态 / 内部实现 / 不冲突原理」四栏的对比表，外加一张排除时序图。如果你能用这张表向一个没读过 PoC 的人讲明白「为什么 fileio 要有两份」，本讲就真正掌握了。

## 6. 本讲小结

- PoC 用文件名后缀 `.v93.vhdl` / `.v08.vhdl` 标注一份源码对应的 VHDL 语言版本；同一逻辑包（如 `FileIO`）的两份实现**同名但互斥**，任意一次编译只会有其中一份参与。
- 「受保护类型（protected type）」在 VHDL-2002 才标准化，因此凡是用了它的包都必须再为 VHDL-93 维护一份「老办法」实现：v93 用包体顶层 `shared variable` + 自由过程（全局单例、无封装），v08 用 `type ... is protected body`（可实例化、封装、原子访问）。
- 读取受保护类型内部状态或 `shared variable` 的函数必须写成 `impure function`，这一点两份 fileio 都遵守。
- `protected.v08.vhdl` 提供 `P_BOOLEAN`/`P_INTEGER` 等受保护版基础类型，是 v08 路线的「工具箱」，且必须在 `fileio.v08` 之前编译（依赖拓扑序）。
- 选哪份文件由 pyIPCMI 在**编译前**读 `.files` 决定，判据是 `VHDLVersion`（阈值 `< 2002`）、`ToolChain`、`Environment`；这和 u3-l2 的 `generate` 厂商选择（展开期、同一文件内）是两个不同层次的可移植机制。
- `.files` 把 `protected.v08` 只放在 `<= 2008` 分支、`< 2002` 分支完全不提它，配合「93 编译器看不懂 protected 语法」，共同保证了不兼容的文件永远不会被错误的编译器看到。

## 7. 下一步学习建议

- **横向对照**：回到 u4-l1（仿真辅助包）重新读一遍 `sim.files`，现在你应该能一眼看出 `sim_unprotected.v93` 与 `sim_protected.v08` 的命名含义，以及它们为何被放在同一个 `if/elseif` 的两支。
- **纵向深入工具链**：本讲反复提到「pyIPCMI 在编译前读 `.files`」。这个 Python 基础设施到底如何解析 `if/elseif`、`vhdl`、`include` 指令，将在 u5-l1（pyIPCMI 基础设施与命令行前端）展开。
- **补全可移植机制的全景**：把本讲的「版本维度（`.files` 选文件）」和 u3-l2 的「厂商维度（`generate` 选实体）」、u2-l3 的「板/器件维度（`MY_DEVICE` 派生 `VENDOR`）」摆在一起，你就集齐了 PoC「一份源码、多处可移植」的三大支柱。
- **动手延伸**：如果你想加深印象，可以尝试为 u2-l2 讲过的 `utils.vhdl` 设想一个「假如某个函数在 93 和 08 下必须不同实现」的场景，自己写一对 `xxx.v93.vhdl` / `xxx.v08.vhdl`，并在一份 `.files` 片段里用 `if (VHDLVersion ...)` 把它们接进去（这只是练习，**不要**改动仓库里的真实源码）。
