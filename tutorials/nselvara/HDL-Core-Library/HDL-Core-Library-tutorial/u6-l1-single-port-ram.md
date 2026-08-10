# 单口 RAM 与非约束端口

## 1. 本讲目标

本讲是「RAM 内存模块」单元的第一讲。我们将从全库最基础的一个存储原语——**单口 RAM**（single-port RAM）——开始，建立读存储器 RTL 的自信。学完本讲你应该能够：

- 理解什么是单口 RAM、它和双口 RAM 的区别在哪里。
- 看懂 VHDL 中**非约束端口**（`address: in unsigned;`、`write_data: in std_ulogic_vector;`）的写法，并解释为什么它能「让一份源码服务任意位宽」。
- 掌握在 `architecture` 内用 `address'length`、`write_data'subtype` 这类属性**反推**出 `RAM_DEPTH`（存储深度）和 `ram_t`（存储数组类型）的技巧。
- 读懂用 `write_and_not_read` 单信号在单端口上仲裁读写、用 `en` 控制时序的进程。
- 看懂那条「刻意在复位里写 `null`」的注释——它是为了引导综合工具推断出 FPGA 片上的 BRAM 资源。

## 2. 前置知识

在进入源码之前，先用通俗语言铺垫几个概念。

**RAM（随机存取存储器，Random Access Memory）** 是一种可以按「地址」读写数据的存储阵列。你可以把它想象成一排带编号的格子：给一个地址编号，就能把数据放进那个格子（写），或者把那个格子里已有的数据取出来（读）。地址的位宽决定了格子的数量，数据的位宽决定了每个格子能放多少比特。

**单口（single-port）与双口（dual-port）** 是按「同时能做几个访问」来分的：

| 类型 | 读口 | 写口 | 一个时钟周期内 |
|---|---|---|---|
| 单口 RAM | 共用一个端口 | 共用一个端口 | 要么读、要么写，不能同时 |
| 双口 RAM | 独立读口 | 独立写口 | 可以同时读和写 |

本讲的 `single_port_ram` 属于前者：同一时刻只能读或只能写。这个限制正是它的名字里「单口」的含义，也是后面 u6-l2（双口 RAM）要解决的问题。

**非约束（unconstrained）类型** 是 VHDL（尤其 VHDL-2008）的一个重要特性。平时我们写信号常常带固定范围，例如 `signal addr : unsigned(7 downto 0);`，这叫「已约束」——位宽在声明时就锁死成 8 位。而非约束类型在声明时**故意不写范围**，例如 `unsigned`（不带括号），把位宽推迟到「真正连上线的那一刻」才确定。本讲的妙处正是把端口的位宽推迟到例化时刻，从而让一份 RAM 源码可以变出 8 位、16 位、任意位宽的版本。

> 本讲承接 u3-l1（`memories_pkg` 与非约束数组类型）。u3-l1 讲的是「包里声明的非约束数组类型 `rom_t`」，本讲把同样的思想用在 **端口** 上——地址和数据端口本身都是非约束的。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 作用 |
|---|---|---|
| [ip/memories/ram/single_port/single_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/single_port_ram.vhd) | 设计源码（可综合） | 本讲的主角，单口 RAM 的全部实现，只有 63 行 |
| [ip/memories/ram/single_port/tb/tb_single_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/tb/tb_single_port_ram.vhd) | 测试台（仅仿真） | 用 VUnit + OSVVM 随机化验证 RAM 的写入/回读、deactivate 等行为 |
| [ip/memories/ram/single_port/tb/tb_single_port_ram.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/tb/tb_single_port_ram.do) | 波形脚本 | ModelSim/QuestaSim 的 Tcl 脚本，用于图形化查看波形 |
| [ip/memories/memories_pkg.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/memories_pkg.vhd) | 共享包（承接 u3-l1） | 定义非约束数组类型 `rom_t`，本讲会与之对比 |

注意目录组织遵循 u1-l2 讲过的约定：设计源码 `single_port_ram.vhd` 放在 IP 子目录根，测试台 `tb_single_port_ram.vhd` 和波形脚本 `.do` 放进 `tb/` 子文件夹并加 `tb_` 前缀，好让 `test_runner.py` 用 `tb_*.vhd` 通配符自动发现。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看非约束端口的设计思想，再看 `RAM_DEPTH`/`ram_t` 如何被反推出来，然后是读写时序控制，最后是那条关于 BRAM 推断的复位注释。

### 4.1 非约束端口：让一份源码服务任意位宽

#### 4.1.1 概念说明

很多存储器 IP 的痛点是：写死 8 位版本，换个项目要 16 位就得改源码、改一堆范围声明、再测一遍。本模块的设计目标是用一份 `single_port_ram.vhd` 同时满足「8 位地址 / 8 位数据」「12 位地址 / 16 位数据」等任意组合，**改源码为零**。

办法就是把端口声明成**非约束的**：地址端口写成 `address: in unsigned;`（不带范围），数据端口写成 `write_data: in std_ulogic_vector;`（不带范围）。这些端口的真实位宽，等别人来例化这个 RAM、用具体信号连上端口的那一刻，才由外部连线「传染」进来并被最终确定。这就是「接口留白、位宽推迟到例化」。

> 与 u3-l1 的对比：u3-l1 的 `rom_t` 是「包里定义的非约束数组类型」，留白的是数组深度和元素位宽；本讲的非约束**端口**留白的是地址位宽和数据位宽。两者用的是同一族 VHDL 思想，只是一个作用在类型定义、一个作用在实体端口。

#### 4.1.2 核心流程

1. `entity` 阶段：把 `address`、`write_data`、`read_data` 都声明成非约束类型，不写范围。
2. 例化阶段：使用方用一个**已约束**的信号（例如 8 位的 `unsigned`）接到 `address` 上，端口的范围就被确定成 `(7 downto 0)`。
3. `architecture` 内部：用属性 `address'length`（地址位宽）、`write_data'subtype`（数据子类型）把刚才「传染」进来的位宽信息反查出来，据此构造存储数组（见 4.2）。

#### 4.1.3 源码精读

先看 `entity` 部分，端口全部是非约束的：

[ip/memories/ram/single_port/single_port_ram.vhd:L13-L23](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/single_port_ram.vhd#L13-L23) —— `entity single_port_ram` 声明了 7 个端口，其中地址和数据端口都没有写范围（注意 `unsigned` 和 `std_ulogic_vector` 后面都没有 `(x downto y)`），这正是非约束端口。读端口 `read_data` 同样非约束，它的最终位宽由使用方连线决定。

关键三点：

- `address: in unsigned;` —— 地址是非约束无符号整数向量，位宽待定。
- `write_data: in std_ulogic_vector;` —— 写入数据非约束，位宽待定。
- `read_data: out std_ulogic_vector;` —— 读出数据非约束，位宽待定；通常和 `write_data` 位宽一致。

对比测试台里的例化方式就能看到「位宽是怎么被传染进来的」：

[ip/memories/ram/single_port/tb/tb_single_port_ram.vhd:L52-L61](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/tb/tb_single_port_ram.vhd#L52-L61) —— 测试台声明了常量 `ADDRESS_WIDTH: positive := 8` 和 `DATA_WIDTH: positive := 8`，并用它们约束出 8 位的 `address`、`data_in`、`data_out`。这些**已约束**信号随后接到 DUT 端口上，就把 8 位这个维度传给了 RAM。

[ip/memories/ram/single_port/tb/tb_single_port_ram.vhd:L202-L211](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/tb/tb_single_port_ram.vhd#L202-L211) —— 例化 `entity work.single_port_ram` 时，外部信号 `address`（8 位）→ 端口 `address`、`data_in`（8 位）→ `write_data`、`data_out`（8 位）← `read_data`。位宽完全由这些连线决定，RAM 源码本身一行都不用改。

#### 4.1.4 代码实践

**实践目标：** 亲手验证「改例化位宽不改源码」。

**操作步骤：**

1. 打开测试台，把 `ADDRESS_WIDTH` 和 `DATA_WIDTH` 两个常量改成别的值（例如都改成 `16`）。
2. 重新运行仿真（参考 u1-l3 的 `test_runner.py`）。

**需要观察的现象：** 设计源码 `single_port_ram.vhd` 无需任何修改即可编译通过，仿真照常跑。

**预期结果：** 由于测试台里 `test_full_ram` 用 `2**address'length` 遍历所有地址，地址加宽后遍历会变慢，但功能依然正确。这验证了位宽是例化侧决定的，不是源码写死的。

> 如果你没有本地仿真器，可改为「源码阅读型实践」：在 `tb_single_port_ram.vhd` 中确认 `ADDRESS_WIDTH`/`DATA_WIDTH` 仅出现在测试台内部，设计源码 `single_port_ram.vhd` 里搜不到任何 `8` 或 `downto 0` 之类的硬编码位宽。

#### 4.1.5 小练习与答案

**练习 1：** 为什么不能在 `entity` 里给 `address` 写死 `unsigned(7 downto 0)`？

> **答案：** 一旦写死，位宽就在实体里锁死了，每次换位宽都要改源码并重新验证。非约束写法把位宽决定权交给例化方，让一份源码服务任意位宽，这正是「可复用 IP 核」的核心收益。

**练习 2：** 如果例化时 `write_data` 接了 16 位、而 `read_data` 接了 8 位信号，会发生什么？

> **答案：** 类型不匹配。两个端口虽然都声明成非约束的 `std_ulogic_vector`，但一旦被外部连线约束成不同位宽，例化处会出现位宽不一致的关联错误，编译期就会报错。所以实际使用时读、写数据端口位宽必须一致。

---

### 4.2 在 architecture 内推导 RAM_DEPTH 与 ram_t

#### 4.2.1 概念说明

端口的位宽被「传染」进来之后，`architecture` 内部怎么知道要建多大的存储阵列？答案是**用属性反查**：

- `address'length`：地址向量的位宽，记为 \(N\)。
- `RAM_DEPTH`：存储深度，即格子的总数，等于 \(2^N\)。
- `write_data'subtype`：写入数据的子类型（含位宽信息），用来决定每个格子多宽。

有了深度和位宽，就能定义出存储数组类型 `ram_t`，并声明一个 `signal ram_reg : ram_t;` 作为真正的存储体。这一切都发生在 `architecture` 的声明区，**在例化时刻才求值**——所以同一份源码会为不同位宽例化出不同大小的 `ram_t`。

#### 4.2.2 核心流程

设地址位宽为 \(N\)，则：

\[ \text{RAM\_DEPTH} = 2^{N} \]

地址可寻址范围是 `0` 到 \(\text{RAM\_DEPTH}-1\)。存储数组类型定义流程：

1. `constant RAM_DEPTH : positive := 2**address'length;` —— 算出深度。
2. `subtype ram_depth_t is natural range 0 to RAM_DEPTH - 1;` —— 定义地址索引子类型，给数组当范围用。
3. `type ram_t is array (ram_depth_t) of write_data'subtype;` —— 用「索引子类型」当数组范围、用「数据子类型」当元素类型。

注意 `ram_t` 是**局部类型**——它只存在于这个 `architecture` 里，每次例化位宽不同，`ram_t` 的形状也不同。

#### 4.2.3 源码精读

这三行是整个模块最精妙的「推导」所在：

[ip/memories/ram/single_port/single_port_ram.vhd:L25-L28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/single_port_ram.vhd#L25-L28) —— 在 `architecture behavioural` 的声明区，三行分别算出 `RAM_DEPTH`（深度）、定义 `ram_depth_t`（索引子类型）、定义 `ram_t`（存储数组类型）。`write_data'subtype` 让数组元素的位宽自动跟随写数据端口。

逐行解释：

- `constant RAM_DEPTH: positive := 2**address'length;` —— 若例化时地址是 8 位，则 `RAM_DEPTH = 2**8 = 256`；若是 12 位，则为 4096。深度完全由地址位宽推出。
- `subtype ram_depth_t is natural range 0 to RAM_DEPTH - 1;` —— 把合法地址索引限定在 `0 .. 255`（8 位时），用作数组下标范围。
- `type ram_t is array (ram_depth_t) of write_data'subtype;` —— 数组有 `RAM_DEPTH` 个元素，每个元素的位宽继承自 `write_data`（8 位数据时，每个元素就是 8 位的 `std_ulogic_vector`）。这和 u3-l1 里 `rom_t is array (natural range <>) of std_ulogic_vector` 是同族写法。

随后 `signal ram_reg: ram_t;` 就是真正的存储体：

[ip/memories/ram/single_port/single_port_ram.vhd:L30-L33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/single_port_ram.vhd#L30-L33) —— 声明 `write_enable`/`read_enable` 两个内部信号，以及存储体 `ram_reg`。

还有一个保护性的断言值得注意：

[ip/memories/ram/single_port/single_port_ram.vhd:L44](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/single_port_ram.vhd#L44) —— `assert RAM_DEPTH <= natural'high` 检查深度没有超出 VHDL `natural`（32 位整数的非负半区，上界约 \(2^{31}-1\)）能表示的范围。如果有人例化了一个 32 位的地址，\(2^{32}\) 会溢出整数范围，这条断言会在仿真一开始就报警（`severity error`），避免后续下标越界。它实质上把可例化的地址位宽限制在约 30 位以内。

#### 4.2.4 代码实践

**实践目标：** 验证改地址位宽后 `RAM_DEPTH` 自动变化。

**操作步骤：**

1. 复制一份测试台（例如 `tb_single_port_ram_wide.vhd`，仅用于本地练习，不要提交），把 `ADDRESS_WIDTH` 改成 `12`，`DATA_WIDTH` 改成 `16`。
2. 在仿真里给 `RAM_DEPTH` 加一条 `report` 打印（例如在进程里写 `report "RAM_DEPTH = " & integer'image(...);`，或直接读断言是否触发）。

> 由于 `RAM_DEPTH` 是 `architecture` 内的局部常量，从测试台侧无法直接「看见」它。变通办法：例化 12 位地址后，遍历地址 `0 .. 4095` 写入并回读，若全部成功则间接证明深度确实变成了 4096（否则越界会在仿真里报错）。

**需要观察的现象：** 12 位地址下，访问地址 `0` 和地址 `4095` 都能成功读写且互不干扰。

**预期结果：** 4096 个单元全部可访问，证明 `RAM_DEPTH` 自动变为 \(2^{12}=4096\)。**待本地验证**（具体打印方式取决于你接入的观测手段）。

#### 4.2.5 小练习与答案

**练习 1：** 若地址位宽是 10，`RAM_DEPTH` 是多少？`ram_depth_t` 的范围呢？

> **答案：** `RAM_DEPTH = 2**10 = 1024`；`ram_depth_t` 的范围是 `0 to 1023`。

**练习 2：** 为什么 `ram_t` 要用 `write_data'subtype` 而不是直接写 `std_ulogic_vector(7 downto 0)`？

> **答案：** 因为数据位宽是例化时才确定的。直接写 `7 downto 0` 就把位宽锁死成 8 位了，丢失了非约束端口的灵活性；用 `write_data'subtype` 才能让元素位宽自动跟随例化。

**练习 3：** 假如有人例化 31 位地址，`RAM_DEPTH <= natural'high` 这条断言会怎样？

> **答案：** \(2^{31} = 2147483648\) 已超过 `natural'high`（\(2^{31}-1\)），而且 `2**31` 在 32 位整数里本身就会溢出/回绕。断言条件不成立，会以 `severity error` 上报，提示 `ADDRESS_WIDTH exceeds the maximum allowed value!`。

---

### 4.3 读写时序控制：write_and_not_read、en 与单端口仲裁

#### 4.3.1 概念说明

单口 RAM 的核心约束是「一个周期只能读或写」。本模块用**一个信号** `write_and_not_read` 来仲裁：它为 `'1'` 时本周期写、为 `'0'` 时本周期读。一个信号同时定义了方向，简洁且互斥。

另外还有一个总使能 `en`：只有 `en` 为真时才允许访问 RAM；`en` 为假时端口「沉默」，既不写也不读。测试台里有专门的用例验证「关掉 `en` 后 RAM 不响应」。

整个读写由一个**同步时序进程**（对 `sys_clk` 敏感、在上升沿动作）驱动，所以写进去的数据在时钟上升沿被锁存，读出的数据也寄存一拍输出。

#### 4.3.2 核心流程

每个时钟上升沿，进程按下面的优先级执行：

```
若 rising_edge(sys_clk):
    若 sys_rst_n == '0' (复位有效):
        什么都不做 (null)            ← 见 4.4，刻意不清空 RAM
    否则 若 en == '1' (使能):
        若 write_enable (write_and_not_read == '1'):
            ram_reg(address) <= write_data   ← 写入
        否则 若 read_enable (write_and_not_read == '0'):
            read_data <= ram_reg(address)    ← 读出（寄存到 read_data）
```

注意 `write_enable` / `read_enable` 不是手动控制的，而是由一个组合进程从 `write_and_not_read` 译出：写使能 = `write_and_not_read`，读使能 = `not write_and_not_read`，两者天然互斥。

#### 4.3.3 源码精读

先看读写使能的译码进程（组合逻辑）：

[ip/memories/ram/single_port/single_port_ram.vhd:L35-L39](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/single_port_ram.vhd#L35-L39) —— `mem_control_proc` 对 `write_and_not_read` 敏感，组合地生成 `write_enable`（等于 `write_and_not_read`）和 `read_enable`（取反）。这等价于两条并发赋值，只是用进程写出来。

再看主操作进程（同步时序）：

[ip/memories/ram/single_port/single_port_ram.vhd:L41-L62](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/single_port_ram.vhd#L41-L62) —— `mem_operation_proc` 对 `sys_clk` 敏感，在上升沿按「复位 → en → 写/读」优先级处理。

几个关键细节：

- `variable address_v: ram_depth_t;` 和 `address_v := to_integer(address);` 把 `unsigned` 地址转成整数下标，用来索引 `ram_reg`。`to_integer` 来自 `ieee.numeric_std`（见文件顶部 `use ieee.numeric_std.all;`）。
- 第 44 行的 `assert` 写在 `if rising_edge` **之外**但仍在进程内，意味着每次 `sys_clk` 翻转（上升沿和下降沿）都会评估一次，起到运行时不变式检查的作用。
- 第 56 行写：`ram_reg(address_v) <= write_data;` —— 信号赋值，新值在进程挂起时生效。
- 第 58 行读：`read_data <= ram_reg(address_v);` —— 读出的是当前 `ram_reg` 的值并寄存到 `read_data`，因此 `read_data` 比 `address` 晚一拍（寄存输出）。
- 因为单口下读写互斥（`write_and_not_read` 只能是一个值），同一个上升沿不可能既写又读，所以不存在 u6-l2（双口 RAM）要处理的「同地址同时读写」冒险。

测试台侧的写后读校验正好体现了「寄存一拍」的节拍：

[ip/memories/ram/single_port/tb/tb_single_port_ram.vhd:L116-L135](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/tb/tb_single_port_ram.vhd#L116-L135) —— `test_full_ram` 的循环：先把 `write_and_not_read <= '1'`、给出地址和数据，等一个时钟周期（写入）；再把 `write_and_not_read <= '0'`，等一个时钟周期（读出，`read_data` 在这拍的上升沿更新）；然后用 `check_equal(data_out, data_in, "data_out")` 校验读回值。它遍历 `0 .. 2**address'length-1` 所有地址逐一写后读。

还有专门验证 `en` 关闭的用例：

[ip/memories/ram/single_port/tb/tb_single_port_ram.vhd:L159-L179](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/tb/tb_single_port_ram.vhd#L159-L179) —— `test_when_ram_deactivated` 把 `en <= '0'`，即便给出写地址和数据并尝试读，`check_relation(data_out /= data_in)` 断言读回值**不等于**写入值，证明 RAM 在未使能时不响应、存储内容未被更新。

#### 4.3.4 代码实践

**实践目标：** 亲手驱动一次「写地址 0~3、再回读校验」。

**操作步骤：**

1. 阅读测试台里的 `wait_sys_clk_cycles`、`restart_module` 两个过程，理解节拍。

[ip/memories/ram/single_port/tb/tb_single_port_ram.vhd:L102-L114](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/tb/tb_single_port_ram.vhd#L102-L114) —— `wait_sys_clk_cycles` 等若干个上升沿后多等一个 `PROPAGATION_TIME`（1 ns），保证寄存输出稳定后再采样；`restart_module` 拉低 `sys_rst_n` 一拍再释放，让 RAM 进入已知初态。

2. 仿照 `test_full_ram` 的写法，**只在地址 0~3** 上写 4 个不同值（如 0x11、0x22、0x33、0x44）再回读。可以写一个新的 `run(...)` 用例。

**需要观察的现象：** 每个地址写入后，下一个读周期能在 `data_out` 上看到刚才写入的值。

**预期结果：** 4 次写后读全部 `check_equal` 通过。如果忘了在写和读之间切 `write_and_not_read`，或在读周期后没等 `PROPAGATION_TIME` 就采样，校验会失败——这能帮你体会「寄存一拍」的时序。

> 如果无法运行仿真，可改为「阅读型实践」：用纸笔跟踪 `test_full_ram` 第一轮迭代（i=0），标出每个时钟沿前后 `write_and_not_read`、`address`、`data_in`、`ram_reg(0)`、`read_data` 的取值，确认回读值正确。

#### 4.3.5 小练习与答案

**练习 1：** 为什么读出的数据比给地址晚一拍？

> **答案：** 因为 `read_data <= ram_reg(address_v)` 是时钟进程里的信号赋值，`read_data` 是寄存器输出。地址在第 N 拍给出，读出的值在第 N+1 拍才出现在 `read_data` 上。测试台的 `wait_sys_clk_cycles` 多等一个 `PROPAGATION_TIME` 正是为了等它稳定。

**练习 2：** 如果同一周期同时想要读和写，单口 RAM 会怎样？

> **答案：** 不会发生。`write_and_not_read` 是单比特信号，同一时刻只能是 `'1'`（写）或 `'0'`（读），读写天然互斥。这正是「单口」的含义。要同周期同时读写，需要 u6-l2 的双口 RAM。

**练习 3：** `en` 为 `'0'` 时，给地址和数据，RAM 内容会变吗？

> **答案：** 不会。`en` 为假时既不进入写分支也不进入读分支，`ram_reg` 保持原值，`read_data` 也不更新。`test_when_ram_deactivated` 正是验证这一点。

---

### 4.4 BRAM 推断与复位策略

#### 4.4.1 概念说明

FPGA 芯片内部有专门的片上存储资源，Xilinx 称为 **BRAM**（Block RAM），Intel 称为 M20K/M10K 等存储块。综合工具（Vivado、Quartus）能把「行为像 RAM」的 RTL 自动**推断**（infer）成这些专用资源，而不是用一堆触发器（寄存器）去拼——后者既费面积又费功耗。

但推断是有条件的。其中一个关键条件是：**不要在复位时一次性清空整块 RAM**。原因是真实 BRAM 硬件通常不支持「单周期把所有单元同时清零」这种操作；如果 RTL 里写了 `for all addresses: ram_reg(addr) <= 0;`，综合工具会认为「这不是普通 BRAM 的行为」，转而用触发器实现，从而浪费资源。

因此本模块在复位分支里**刻意写 `null`**（什么都不做），并在注释里说明这是为了引导 Xilinx 工具推断 BRAM。这是硬件设计里「RTL 写法影响综合结果」的一个典型例子。

#### 4.4.2 核心流程

复位分支的处理逻辑：

```
若 rising_edge(sys_clk):
    若 sys_rst_n == '0' (复位有效):
        null                       ← 刻意不清空整块 RAM
    否则 ... (正常读写)
```

权衡：

- 复位时不清空 → 综合成 BRAM（省资源，但上电后 RAM 内容未定义，需软件/外逻辑初始化）。
- 复位时整块清零 → 可能被综合成触发器阵列（费资源，但上电即已知值）。

本模块选择了前者，并接受「复位后 RAM 内容不确定」这一约定——使用方应在写入有效数据后再去读。

#### 4.4.3 源码精读

[ip/memories/ram/single_port/single_port_ram.vhd:L48-L61](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/single_port_ram.vhd#L48-L61) —— `if rising_edge(sys_clk)` 块内的复位分支。重点看第 49–53 行。

逐行解读注释与代码：

- `if sys_rst_n = '0' then` —— 低有效复位（`sys_rst_n` 名字里的 `_n` 表示 active-low）。
- 注释 `-- NOTE: To infer Xilinx's BRAM, for Intel I don't know` —— 作者说明：为了让 Xilinx 工具推断出 BRAM 才这么写；Intel 侧的情况作者当时不确定（原样保留）。
- 注释 `-- the reset can only be done one address a clock cycle or the reset is completely left out which saves resources.` —— 解释两种可接受的复位方式：要么每周期只清一个地址（慢），要么干脆完全不清（省资源）。本模块选择了「完全不清」。
- `null;` —— 空语句，复位分支什么都不做，从而不破坏 BRAM 推断。

一个有意思的推论：因为复位不清 RAM，且 RAM 上电内容未定义，测试台才需要 `restart_module` 之后**先写后读**来建立已知状态——它从不假设复位后 RAM 是 0。这也是 `test_full_ram` 先写再回读的原因。

#### 4.4.4 代码实践

**实践目标：** 体会「复位不清 RAM」对使用方的影响。

**操作步骤：**

1. 阅读测试台 `restart_module` 与 `test_full_ram`，注意它总是「写完再读」，从不直接读一个「没写过的地址」并期望它是 0。

[ip/memories/ram/single_port/tb/tb_single_port_ram.vhd:L109-L114](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/single_port/tb/tb_single_port_ram.vhd#L109-L114) —— `restart_module` 仅复位模块（让读端口/使能逻辑进入已知态），并不清空存储体。

2. 想象一个反例：如果在本模块的复位分支里写一个 `for` 循环清零整块 RAM，综合结果会怎样？

**需要观察的现象（思想实验，待本地综合验证）：** 把复位分支改成清空整块 RAM 后，用 Vivado/Quartus 综合看资源报告。

**预期结果：** 原版（`null`）应推断出 BRAM 资源；改成整块清零的版本可能退化成大量触发器（FF）占用。**待本地验证**（需要厂商综合工具）。

> 如果没有综合工具，可改为「阅读型实践」：在源码注释里圈出作者关于 Intel 的不确定性（`for Intel I don't know`），并思考——Intel 的存储块对复位的要求可能与 Xilinx 不同，移植时需要查对应工具手册确认。

#### 4.4.5 小练习与答案

**练习 1：** 为什么复位分支写 `null` 而不是清空整块 RAM？

> **答案：** 为了让综合工具把 `ram_reg` 推断成片上 BRAM。真实 BRAM 硬件通常不支持单周期整块清零，如果 RTL 这么写，工具会改用触发器实现，浪费面积和功耗。

**练习 2：** 复位后立即读一个从未写过的地址，能得到什么值？

> **答案：** 不确定（未定义）。因为复位不清 RAM，上电后存储内容是任意的。使用方必须先写后读，不能假设初值是 0。这也解释了测试台为什么总是先写再回读。

**练习 3：** 注释提到「the reset can only be done one address a clock cycle」，这是什么意思？

> **答案：** 如果确实需要复位存储内容，BRAM 友好的做法是「每个周期只清一个地址」，用一个计数器逐地址清零（耗时 `RAM_DEPTH` 个周期）。本模块连这种方式都没用，而是完全不清，以最大化省资源并简化逻辑。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个贯穿性小任务。

**任务：** 用 16 位数据、12 位地址例化一版 `single_port_ram`，并向地址 0~3 写入 4 个不同的 16 位值再回读校验，最后论证 `RAM_DEPTH` 已自动变为 4096。

**建议步骤：**

1. **例化：** 仿照 `tb_single_port_ram.vhd`，新建一个练习用测试台，把 `ADDRESS_WIDTH` 设为 `12`、`DATA_WIDTH` 设为 `16`，相应声明：
   ```vhdl
   signal address : unsigned(11 downto 0) := (others => '0');
   signal data_in : std_ulogic_vector(15 downto 0) := (others => '0');
   signal data_out: std_ulogic_vector(15 downto 0);
   ```
   （示例代码，仅供练习，不要提交到仓库。）

2. **写后读 4 个地址：** 仿照 `test_full_ram` 的循环结构，但把范围限定在 `0 .. 3`，写入 4 个不同的 16 位值（如 `x"1111"`、`x"2222"`、`x"3333"`、`x"4444"`），每个地址写一个周期、读一个周期，用 `check_equal(data_out, data_in)` 校验。

3. **论证深度：** 回顾 4.2 的公式 \(\text{RAM\_DEPTH} = 2^{N}\)，\(N=12\) 时 \(\text{RAM\_DEPTH}=4096\)。你无法从测试台直接「看见」`RAM_DEPTH`，但可以额外尝试访问地址 `4095`（最高地址）：若能成功写后读，则间接证明深度至少达到 4096；若再访问 `4096` 会因 `ram_depth_t` 范围越界而出错——这反向印证了深度上限正是 4096。

4. **（可选）复位观察：** 在写入前先读一次地址 0，观察 `read_data` 是任意值（未定义），印证 4.4「复位不清 RAM」的结论。

**预期结果：** 4 个地址写后读全部通过；地址 `4095` 可访问；`RAM_DEPTH` 自动为 4096。**待本地验证**（具体取决于你的仿真器与观测手段）。

## 6. 本讲小结

- `single_port_ram` 是一个单端口 RAM：同一周期只能读或写，由 `write_and_not_read` 单信号仲裁方向，`en` 控总使能。
- 端口 `address: in unsigned;`、`write_data/read_data: in/out std_ulogic_vector;` 都是**非约束**的，位宽推迟到例化时刻由外部连线决定，使一份源码服务任意位宽。
- 在 `architecture` 内用 `address'length` 推出 `RAM_DEPTH = 2**address'length`，用 `write_data'subtype` 定义元素位宽，从而构造出局部类型 `ram_t`；`assert RAM_DEPTH <= natural'high` 防止地址过宽导致整数溢出。
- 读写由一个对 `sys_clk` 敏感的同步进程驱动，读出是**寄存一拍**的输出；单口下读写互斥，不存在同周期读写冒险。
- 复位分支刻意写 `null`（不清空整块 RAM），是为了引导综合工具把 `ram_reg` 推断成片上 BRAM；代价是复位后内容未定义，使用方须先写后读。

## 7. 下一步学习建议

本讲建立了「非约束端口 + 内部推导存储类型 + 同步读写」的基本范式。接下来：

- **u6-l2 双口 RAM 与读写顺序：** 当读口和写口分离后，会出现「同地址同时读写」的新问题。下一讲会讲解 `dual_port_ram` 如何用**变量型**（`variable`）存储实现同周期 read-before-write，去对比本讲用 `signal` 的写法。
- **u6-l3 双时钟双口 RAM：** 进一步把读和写分别用独立时钟驱动，这是后续异步 FIFO 的存储底座。
- **回头看 u3-l1：** 如果你对 `type ... is array (...) of std_ulogic_vector` 这种非约束数组类型还不够熟，可以重读 `memories_pkg` 与 `rom_t`，和本讲的 `ram_t` 对照理解。
- **建议阅读的源码：** 在进入下一讲前，先打开 `ip/memories/ram/dual_port/dual_port_ram.vhd` 扫一眼，找出它和本讲的两个核心区别——双口结构、以及是否用了 `variable` 存 RAM。
