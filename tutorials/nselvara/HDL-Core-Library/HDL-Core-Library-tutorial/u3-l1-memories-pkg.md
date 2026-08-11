# memories_pkg 与非约束数组类型

## 1. 本讲目标

本讲是「共享工具包与基础设施」单元的第一讲。读完本讲，你应当能够：

- 说出 VHDL 的 **package（包）** 机制解决了什么问题，并能写出 `package` + `package body` 的最小骨架。
- 看懂本库共享包 `memories_pkg` 中 `rom_t` 这一**非约束数组类型**的定义，解释它为什么需要 VHDL-2008。
- 解释 `rom_t` 这「两对括号」分别约束什么（深度与位宽），并看懂 `rom.vhd` 如何只用一个 `rom_t` 就构造出**任意深度、任意位宽**的常量 ROM 表。

本讲承接 [u2-l1 同一实体多架构模式](u2-l1-multi-architecture-pattern.md)：你已经会读 entity + 多套 architecture，现在我们把镜头拉到「被多个模块共享的那一小撮类型定义」上。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个概念。

- **类型（type）与子类型（subtype）**：类型规定了一类数据长什么样（比如「一串 bit」）；子类型是在某个类型上加一条限制（比如「8 位的 bit 串」）。`std_ulogic_vector` 是一个类型，`std_ulogic_vector(7 downto 0)` 是它的一个被约束的用法。
- **非约束（unconstrained）**：声明类型时**故意不写死范围**，把范围留到「真正声明一个变量/信号」时再给。`std_ulogic_vector` 本身就是非约束的——它没说自己是几位，位宽由你用时决定。
- **数组（array）**：把一堆同类型元素排成一排。数组有两个维度要关心：**有多少个元素（深度）**，以及**每个元素是几位（位宽）**。
- **package（包）**：VHDL 里用来存放「共享定义」的容器——类型、常量、函数、元件例化声明都可以放进去，别的文件用一句 `use work.xxx.all;` 就能复用，避免到处复制粘贴。
- **work 库**：VHDL 的默认工作库。你编译进当前项目的所有设计单元都在 `work` 里，互相可见。

一个关键直觉：**如果每次造一个 ROM 都得先把「深度×位宽」写死在类型里，那这个类型就只能在一种配置下用。** 本库要的是「一份代码、任意容量」，这就引出了本讲的核心——把数组的两个维度都留空。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [ip/memories/memories_pkg.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/memories_pkg.vhd) | 设计·共享包 | 整个 `rom_t` 类型只有这一处定义，全文仅 19 行 |
| [ip/memories/rom/rom.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd) | 设计·存储模块 | 演示 `rom_t` 如何被约束成任意深度/位宽的常量 ROM 表 |
| [ip/memories/rom/tb/tb_rom.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/tb/tb_rom.vhd) | 测试台 | 用同样的「两对括号」约束 `rom_t`，强化理解 |

> 提醒：`memories_pkg.vhd` 放在 `ip/memories/` 顶层（而不是塞进 `rom/` 子文件夹），因为它是**整个存储大类共享**的包——`fifo/`、`ram/`、`rom/` 都可能用到它（例如 [ip/memories/fifo/fifo_async.vhd:L14](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L14) 里就有 `use work.memories_pkg.all;`）。这一点在 [u1-l2 仓库目录结构](u1-l2-directory-structure.md) 已说明。

## 4. 核心概念与源码讲解

### 4.1 memories_pkg：共享类型包与 package/package body 骨架

#### 4.1.1 概念说明

`memories_pkg` 是本库**自带**的一个共享包（README 把它列为「Memory-related constants and types」，见 [README.md:L42](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L42)）。它解决的问题很朴素：

> 当 `rom.vhd`、`fifo_async.vhd`、测试台 `tb_rom.vhd` 都需要用到「一个 ROM 数组」这种类型时，这个类型应该定义在哪里？

答案就是定义在一个**公共包**里，谁需要谁 `use`。这样类型只有一处定义（单一真相源），改一处全处生效。

一个 VHDL 包由两部分组成：

- **package 声明（package declaration）**：对外可见的「接口」，放类型声明、常量声明、函数**签名**等。
- **package body（包体）**：隐藏的「实现」，放函数/过程的**函数体**、以及「延迟常量（deferred constant）」的真实取值。

#### 4.1.2 核心流程

一个包从编写到被使用，流程是：

1. 在 `memories_pkg.vhd` 里写 `package memories_pkg is ... end package;`，声明共享类型。
2. （可选）写 `package body memories_pkg is ... end package body;`，放实现。
3. 用 `vcom`/`ghdl`/NVC 等把它编译进 `work` 库。
4. 别的设计文件写 `use work.memories_pkg.all;`，即可见包内全部公开内容。

> 什么时候**必须**写 package body？只有当你声明了**延迟常量**（声明里只写类型不写值，值放进 body）或**函数/过程**（签名在声明里、函数体在 body 里）时。如果包里**只放类型声明**，body 可以完全为空，甚至干脆不写——`memories_pkg` 就属于这种情况。

#### 4.1.3 源码精读

先看上下文子句与包声明：

[ip/memories/memories_pkg.vhd:L9-L15](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/memories_pkg.vhd#L9-L15) —— 在声明 `rom_t` 之前，先用 `use ieee.std_logic_1164.all` 让 `std_ulogic_vector` 可见，否则包内无法引用它。

[ip/memories/memories_pkg.vhd:L13-L15](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/memories_pkg.vhd#L13-L15) —— 包声明本体。整段只有一句 `type rom_t is ...`（下一节细讲）。

再看包体：

[ip/memories/memories_pkg.vhd:L17-L19](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/memories_pkg.vhd#L17-L19) —— `package body` 里**什么都没有**。这正是「只声明类型、无需实现」的典型形态。它被保留下来，更多是给将来扩展（哪天要加个共享函数）留位置。

最后看消费方如何引用：

[ip/memories/rom/rom.vhd:L13](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L13) —— `use work.memories_pkg.all;`，一句就让 `rom_t` 在 `rom.vhd` 里可见。`work` 指向当前工作库。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是体会「包被谁共享」。

1. **实践目标**：确认 `memories_pkg` 在全库的可见消费关系。
2. **操作步骤**：在 `ip/` 目录下搜索 `use work.memories_pkg.all`，列出所有命中文件。
3. **需要观察的现象**：哪些设计文件、哪些测试台引用了它。
4. **预期结果**：至少命中 `ip/memories/rom/rom.vhd`、`ip/memories/rom/tb/tb_rom.vhd`、`ip/memories/fifo/fifo_async.vhd` 三处（以本地实际搜索结果为准）。
5. 如果无法本地运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`memories_pkg` 的 `package body` 是空的，能直接删掉吗？为什么？

> **参考答案**：能删。当包里只声明类型（没有延迟常量、没有子程序体）时，body 不是必需的。这里保留是为了将来扩展方便。

**练习 2**：`use work.memories_pkg.all;` 里的 `work` 是什么？

> **参考答案**：`work` 是 VHDL 的默认工作库名。编译进当前项目的所有设计单元都落在这个库里，互相之间用 `work.<单元名>` 即可引用。

---

### 4.2 rom_t：VHDL-2008 非约束数组类型（核心）

#### 4.2.1 概念说明

本讲真正的重点是这一行：

```vhdl
type rom_t is array (natural range <>) of std_ulogic_vector;
```

`rom_t` 是一个数组类型，它的**两个维度都被故意留空**：

1. **深度维度**：`array (natural range <>)` —— 数组的索引是 `natural`（非负整数）的某个子范围，`<>` 表示「范围待定」。到底是 16 项还是 65536 项？定义时不说，用时再给。
2. **位宽维度**：`of std_ulogic_vector` —— 每个元素是一个 `std_ulogic_vector`，而 `std_ulogic_vector` **本身也是非约束的**（没带范围）。每个元素是 8 位还是 12 位？也留到用时再说。

「数组的元素类型本身又是非约束类型」这种写法，是 **VHDL-2008** 才正式允许的（见 README 标注的「VHDL-2008 Compatible」，[README.md:L47](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L47)）。在更早的 VHDL-87/93 里，数组声明要求元素类型**已经完全约束**，你必须写成固定尺寸：

```vhdl
-- VHDL-93 风格：深度 16、位宽 8，写死后只能服务一种配置
type rom_t is array (0 to 15) of std_ulogic_vector(7 downto 0);
```

本库要的是「一个类型服务所有容量的 ROM」，所以借助 VHDL-2008 把两个维度都放开。

#### 4.2.2 核心流程

要从一个非约束的 `rom_t` 得到一个能用的具体对象，需要在声明时**一次性约束两个维度**，语法是「两对括号」：

```vhdl
variable rom : rom_t(0 to 15)(7 downto 0);
--                 ^^^^^^^^  ^^^^^^^^^
--                 第一对：   第二对：
--                 约束深度   约束位宽
```

- 第一对 `(0 to 15)` 约束**数组索引范围**（决定深度，这里 16 项）。
- 第二对 `(7 downto 0)` 约束**元素子类型**（决定每个 `std_ulogic_vector` 的位宽，这里 8 位）。

存储总量可写成：

\[
\text{总比特数} = \underbrace{|R|}_{\text{深度（元素个数）}} \times \underbrace{W}_{\text{每个元素的位宽}}
\]

对上例：\(16 \times 8 = 128\) bit。

为什么需要两对括号而不是一对？因为 `rom_t` 在类型定义里有**两个**未决之处（索引范围、元素范围），VHDL 语法允许在一个类型标记后依次给出多个索引约束，按从外到内的顺序分别落实。

#### 4.2.3 源码精读

[ip/memories/memories_pkg.vhd:L14](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/memories_pkg.vhd#L14) —— `rom_t` 的唯一一处定义。注意 `std_ulogic_vector` 后面**没有** `(range)`，这是位宽维度留空的源码证据。

再看测试台里同样的「两对括号」用法，巩固印象：

[ip/memories/rom/tb/tb_rom.vhd:L117](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/tb/tb_rom.vhd#L117) —— `variable rom_reg_random: rom_t(0 to 2**ADDRESS_WIDTH - 1)(q'range);`。这里用 `2**ADDRESS_WIDTH - 1` 算出深度上界、用 `q'range`（输出端口 `q` 的范围）决定位宽。

[ip/memories/rom/tb/tb_rom.vhd:L99](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/tb/tb_rom.vhd#L99) —— 给 DUT 内部信号起的 `alias`，也用 `rom_t(0 to 2**ADDRESS_WIDTH - 1)(q'range)` 约束。两处写法一致，说明这是本库处理 `rom_t` 的固定范式。

#### 4.2.4 代码实践

这是本讲的**主实践**：仿照 `memories_pkg`，自己定义一个非约束数组类型包，并体会「两对括号」。下面的代码是**示例代码（练习用，非项目原有文件）**，请你在本地新建文件录入。

1. **实践目标**：亲手定义一个 `ram_data_t` 非约束数组类型，并在一个最小 entity 里用「两对括号」声明该类型的常量数组，编译通过即可。
2. **操作步骤**：
   - 新建包文件 `my_mem_pkg.vhd`（示例代码）：

     ```vhdl
     -- 示例代码：练习用包文件（非项目原有文件）
     library ieee;
     use ieee.std_logic_1164.all;

     package my_mem_pkg is
         type ram_data_t is array (natural range <>) of std_ulogic_vector;
     end package;

     package body my_mem_pkg is
     end package body;
     ```

   - 新建一个最小 entity `mini_rom.vhd`（示例代码），用两对括号约束你的类型：

     ```vhdl
     -- 示例代码：练习用 entity（非项目原有文件）
     library ieee;
     use ieee.std_logic_1164.all;
     use ieee.numeric_std.all;
     use work.my_mem_pkg.all;

     entity mini_rom is
     end entity;

     architecture sim of mini_rom is
         -- 同时约束「深度」和「位宽」两个维度：4 个 8 位字
         constant INIT : ram_data_t(0 to 3)(7 downto 0) :=
             (0 => x"0A", 1 => x"5C", 2 => x"FF", 3 => x"00");
     begin
         -- 仅用于演示类型约束能编译通过，无实际端口行为
     end architecture;
     ```

   - 用你本地的仿真器（GHDL/NVC/ModelSim）先编译包、再编译 entity。
3. **需要观察的现象**：编译器是否接受 `ram_data_t(0 to 3)(7 downto 0)` 这种「两对括号」写法。
4. **预期结果**：在 VHDL-2008 模式下编译通过、无报错；`INIT` 被识别为一个 4 项、每项 8 位的常量数组。
5. 如果你的工具默认不是 VHDL-2008，请加上对应开关（如 GHDL 的 `--std=08`、NVC 的 `-2008`）；若一时无法运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`rom_t(0 to 15)(7 downto 0)` 里的两对括号分别约束什么？

> **参考答案**：第一对 `(0 to 15)` 约束数组索引范围，决定深度为 16；第二对 `(7 downto 0)` 约束每个元素 `std_ulogic_vector` 的位宽为 8。

**练习 2**：为什么「数组的元素也是非约束类型」这种写法需要 VHDL-2008？

> **参考答案**：VHDL-87/93 要求数组声明时元素类型必须已经完全约束。允许元素类型本身非约束（即「array of unconstrained」）是 IEEE 1076-2008 引入的特性。本库大量依赖它，所以要求 VHDL-2008。

**练习 3**：给定 `signal r : rom_t(0 to 7)(3 downto 0);`，该信号共存储多少 bit？

> **参考答案**：8 个元素 × 每个 4 位 = 32 bit。

---

### 4.3 rom_t 在 rom.vhd 中的应用：构造任意深度的常量 ROM 表

#### 4.3.1 概念说明

只看类型定义还不够，关键是看它**怎么被用活**。`rom.vhd` 用一个 `rom_t` 就能表达「任意深度、任意位宽」的 ROM，靠的是把约束推迟到**例化时刻**：让深度从地址端口宽度推导，让位宽从数据端口宽度推导。本模块不展开 ROM 的文件初始化细节（那是 [u7-l1 ROM 与文件初始化](u7-l1-rom-file-initialization.md) 的主题），只聚焦「`rom_t` 如何被约束成具体尺寸」。

#### 4.3.2 核心流程

`rom.vhd` 让一个 `rom_t` 落地为具体尺寸的步骤：

1. entity 端口 `address : in unsigned;`（非约束）和 `q : out std_ulogic_vector(DATA_WIDTH-1 downto 0)`（位宽由 generic 决定）——尺寸都不在本模块写死。
2. 用户在顶层例化时给 `address` 连一个具体位宽（如 16 位），端口才被「钉死」。
3. architecture 内部用属性推导：
   - 深度 = `2 ** address'length`（`address'length` 是地址位宽）。
   - 位宽 = `q'range`（输出端口的范围）。
4. 用这两个推导结果去约束 `rom_t`，得到一个具体的常量 ROM 表 `rom_reg`。
5. 运行时按 `address` 索引该表，把对应字送到 `q`。

#### 4.3.3 源码精读

[ip/memories/rom/rom.vhd:L22-L26](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L22-L26) —— 端口声明。注意 `address : in unsigned;` 后面**没有范围**，是一个非约束端口，它的真实位宽由例化方决定。

[ip/memories/rom/rom.vhd:L30-L31](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L30-L31) —— `constant ROM_DEPTH: natural := 2**address'length;`。`address'length` 在 architecture 里求值（此时端口已被例化约束），得到深度；并据此定义子类型 `rom_depth_t` 作为合法地址范围 `0 to ROM_DEPTH-1`。

[ip/memories/rom/rom.vhd:L36](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L36) —— `variable rom_reg: rom_t(rom_depth_t)(q'range) := (others => (others => '0'));`。这是 `rom_t` 被**双维度约束**的核心一行：深度维度用子类型 `rom_depth_t`（即 `0 to ROM_DEPTH-1`），位宽维度用 `q'range`（输出端口的范围）。初始化为全 0。

[ip/memories/rom/rom.vhd:L57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L57) —— `constant rom_reg: rom_t := load_rom(path => MEM_INIT_FILE_PATH);`。函数 `load_rom` 返回一个已经约束好的 `rom_t`（其内部正是上一行的双维度约束），在 **elaboration（精化）期**一次性求值成常量 ROM 表。因为是 `constant`，运行时不可改写。

[ip/memories/rom/rom.vhd:L59](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L59) —— `signal rom_reg_only_for_simulation: rom_reg'subtype := ...;`。这里用 `rom_reg'subtype` 直接「继承」常量 `rom_reg` 的已约束子类型，省得再写一遍两对括号。

[ip/memories/rom/rom.vhd:L71](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L71) —— `q <= rom_reg(to_integer(address));`。运行时按 `address` 索引常量表，把对应字送到输出。

> 设计收益一览：同一份 `rom.vhd` 源码，例化时把 `address` 连成 16 位、`DATA_WIDTH` 设 8 → 得到 65536×8 的 ROM；连成 10 位、`DATA_WIDTH` 设 12 → 得到 1024×12 的 ROM。深度和位宽都靠端口推导，**源码一字不改**。这就是非约束类型带来的复用力。

#### 4.3.4 代码实践

这是一个**源码阅读 + 推导型实践**，目标是亲手验证「尺寸由端口推导」。

1. **实践目标**：给定两组例化配置，手算 `ROM_DEPTH` 与每个元素的位宽，确认同一份源码能服务两种容量。
2. **操作步骤**：对照 [rom.vhd:L30](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L30) 与 [rom.vhd:L36](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L36)，填写下表：

   | 例化配置 | `address'length` | `ROM_DEPTH`（深度） | `q'range` 决定的位宽 | 总存储 bit |
   |---|---|---|---|---|
   | `address` 16 位，`DATA_WIDTH=8`  | 16 | ? | 8 | ? |
   | `address` 10 位，`DATA_WIDTH=12` | 10 | ? | 12 | ? |

3. **需要观察的现象**：你的手算结果与公式 `2**address'length × DATA_WIDTH` 是否一致。
4. **预期结果**：第一行深度 65536、总 524288 bit；第二行深度 1024、总 12288 bit。
5. 若想进一步确认，可在 `tb_rom.vhd` 里把 `ADDRESS_WIDTH`/`DATA_WIDTH` 改成上表两组值各跑一次（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`rom.vhd` 里的 `ROM_DEPTH` 是怎么得到的？为什么不能写成固定常量？

> **参考答案**：`ROM_DEPTH := 2 ** address'length`，由地址端口的位宽在例化后推导。写死就无法让同一份源码支持任意深度的 ROM。

**练习 2**：`variable rom_reg: rom_t(rom_depth_t)(q'range)` 中，深度和位宽分别借用了什么？

> **参考答案**：深度维度借用了子类型 `rom_depth_t`（即 `0 to ROM_DEPTH-1`）；位宽维度借用了输出端口 `q` 的范围 `q'range`。

---

## 5. 综合实践

把本讲三块知识（**包机制 + 非约束数组类型 + 端口推导尺寸**）串起来，做一个最小查表器（**示例代码，非项目原有文件；只需编译通过即可**）。

**任务**：写一个 `tiny_lut`（查找表）entity，要求：

1. 自己定义一个包 `my_mem_pkg`，里面声明非约束数组类型 `lut_data_t`（仿 `rom_t`）。
2. `tiny_lut` 的 `address` 端口写成**非约束**的 `unsigned`，输出 `q` 写成 `std_ulogic_vector(DATA_WIDTH-1 downto 0)`。
3. 在 architecture 内用 `2 ** address'length` 推导深度，用 `q'range` 推导位宽，约束出一个 `constant` 查找表（内容随意，比如递增序列）。
4. 时钟进程里按 `address` 索引表、送到 `q`。

参考骨架（示例代码）：

```vhdl
-- 示例代码：综合练习骨架（非项目原有文件）
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.my_mem_pkg.all;          -- 内含 lut_data_t（写法同 rom_t）

entity tiny_lut is
    generic (DATA_WIDTH: positive := 8);
    port (
        sys_clk  : in  std_ulogic;
        address  : in  unsigned;                       -- 非约束：位宽由例化决定
        q        : out std_ulogic_vector(DATA_WIDTH-1 downto 0)
    );
end entity;

architecture behavioural of tiny_lut is
    constant LUT_DEPTH : natural := 2 ** address'length;     -- 深度由地址位宽推导
    subtype  lut_index_t is natural range 0 to LUT_DEPTH-1;
    -- 双维度约束：深度用子类型，位宽用 q'range
    constant LUT : lut_data_t(lut_index_t)(q'range) := (others => (others => '0'));
    -- ↑ 内容先填全 0，能编译通过即可；想丰富内容可逐项赋值
begin
    process (sys_clk) begin
        if rising_edge(sys_clk) then
            q <= LUT(to_integer(address));
        end if;
    end process;
end architecture;
```

**验收**：在 VHDL-2008 模式下，先编译包再编译 entity，两者均无报错即通过。如果你把 `address` 例化成不同位宽，能体会到「源码不改、容量随例化变化」的复用效果（**待本地验证**）。

## 6. 本讲小结

- `memories_pkg` 是本库**自带**的共享包，用 `package` + `package body` 骨架存放存储大类共享的类型；只声明类型时 body 可为空。
- 核心类型 `rom_t is array (natural range <>) of std_ulogic_vector` 是一个**双维度非约束**数组类型，依赖 **VHDL-2008** 的「数组元素也可非约束」特性。
- 使用时要用「两对括号」一次性约束：第一对约束深度（索引范围），第二对约束位宽（元素子类型）。
- `rom.vhd` 把约束推迟到例化时刻：深度由 `2 ** address'length` 推导、位宽由 `q'range` 推导，从而用一份源码服务任意容量。
- `rom_reg'subtype` 这种写法可以「继承」一个已约束对象的子类型，避免重复书写两对括号。
- `constant rom_reg := load_rom(...)` 在 elaboration 期求值成常量 ROM 表，运行时只读不可改。

## 7. 下一步学习建议

- 下一讲 [u3-l2 utils_pkg 工具函数与 vhdl_utils 子模块](u3-l2-utils-pkg-and-submodule.md) 会讲另一个贯穿全库的包 `utils_pkg`，并把它和本讲的 `memories_pkg` 做对比——前者来自**外部 git 子模块**，后者是**本仓库自带**，两者工程组织方式不同，值得并列理解。
- 想深入了解 `load_rom` 如何用 `textio`/`hread` 从 hex 文件填充 ROM、以及 `SIMULATION_MODE` 的 force 覆盖机制，请阅读 [u7-l1 ROM 与文件初始化](u7-l1-rom-file-initialization.md)。
- 想看非约束类型在 RAM 上的更多用法（如用 `address'length` 推导 RAM 深度），可预习 [u6 RAM 内存模块](u6-l1-single-port-ram.md)。
