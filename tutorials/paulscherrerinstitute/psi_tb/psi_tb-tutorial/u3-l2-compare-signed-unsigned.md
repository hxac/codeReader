# signed/unsigned 比较与容差、IndexString

## 1. 本讲目标

本讲是「比较与检查助手」的第二讲，承接 u3-l1 讲过的 `IntCompare`/`RealCompare`/`StdlCompare`/`StdlvCompareInt`/`StdlvCompareStdlv`。上一讲结尾留下了一个明确的痛点：`StdlvCompareInt` 的错误消息里，十六进制固定按 32 位显示，对超过 32 位的数据「力不从心」。本讲就来填这个坑。

学完本讲你应该能够：

- 理解 `SignCompare`、`SignCompare2`、`UsignCompare` 三者的差异，知道什么时候该用哪一个。
- 看懂 `SignCompareInt`、`UsignCompareInt` 如何通过一行调用复用 `StdlvCompareInt`，并清楚它们继承了哪些限制。
- 会用 `IndexString` 在 `for` 循环里给每一次比较打上下标标签，让批量检查的失败消息一目了然。
- 能够解释「比较本身」与「错误消息」为什么是两件事——一个可以全位宽正确，另一个却被 32 位整数拖累。

## 2. 前置知识

本讲默认你已经掌握 u3-l1 的内容，这里只做最关键的回顾：

- **统一骨架**：所有比较过程都是 `assert <通过条件> report Prefix & Msg & "[Expected..., Received..., Tolerance...]" severity error` 的形式。
- **统一前缀**：`Prefix` 默认是 `"###ERROR###: "`，与 CI 里的 `run_check_errors "###ERROR###"` 构成契约（见 u1-l3），所以一次比较失败就等于一次 CI 失败。
- **`severity error` 只打印不中断**：失败不会立刻停止仿真，而是把错误消息打到 Transcript，让你一次跑完看到全部不匹配。失败判定靠 `###ERROR###` 子串，而不是 severity 级别。
- **容差带**：比较的通过条件是实际值落在期望值附近的一个区间内：

\[
A \in [\,E - T,\ E + T\,]
\]

  其中 \(E\) 是期望值（Expected），\(A\) 是实际值（Actual），\(T\) 是容差（Tolerance）。

- **`StdlvCompareInt` 的 32 位限制**：它把 `Actual` 向量先用 `to_integer` 转成 `integer`，再和 `Expected`（也是 `integer`）比；而错误消息里的十六进制来自一个 `std_logic_vector(31 downto 0)` 的临时变量。VHDL 的 `integer` 类型标准上是 32 位有符号，范围 \(-2^{31} \ldots 2^{31}-1\)，所以一旦被比较的数据超过这个范围，比较和消息都会出问题。这正是本讲要解决的问题。

另外需要澄清一个容易混淆的点：VHDL 的 `signed`、`unsigned`（来自 `ieee.numeric_std`）和 `std_logic_vector` 是**同一组比特的不同类型视图**。同一个 40 比特存储，你可以用 `signed(...)` / `unsigned(...)` / `std_logic_vector(...)` 在三种视图之间显式转换。它们的比特内容相同，区别只在于「编译器如何解释这些比特」：`signed` 按二进制补码、`unsigned` 按无符号、`std_logic_vector` 就是裸比特不做数值解释。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里，只额外引用底座文本包来解释 `to_string` 和 `hstr`。

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_compare_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd) | 本讲主角：`SignCompare`、`SignCompare2`、`UsignCompare`、`SignCompareInt`、`UsignCompareInt`、`IndexString` 的声明与实现都在这里。同时复用上一讲的 `StdlvCompareInt`。 |
| [hdl/psi_tb_txt_util.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd) | 底座文本包。本讲要精确理解其中 `to_string(signed/unsigned)` 与 `hstr(std_logic_vector)` 两个函数，因为它们决定了 `SignCompare` 与 `SignCompare2` 错误消息的「位宽天花板」。 |

> 说明：psi_tb 目前只注册了一个 testbench（`psi_tb_i2c_pkg_tb`，见 `sim/config.tcl` 第 41 行的 `create_tb_run`），并没有 `compare_pkg` 的专用测试平台。所以本讲的实践以「源码阅读 + 自写最小 testbench」为主，是「源码阅读型实践」与「动手型实践」的结合。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 SignCompare / UsignCompare**：有符号/无符号对拍，比较全位宽正确，消息走十进制。
- **4.2 SignCompare2**：比较逻辑不变，但消息改成十六进制，突破 32 位限制。
- **4.3 SignCompareInt / UsignCompareInt**：期望值是整数、实际值是 signed/unsigned，一行复用 `StdlvCompareInt`。
- **4.4 IndexString**：循环里给每条比较消息打上下标 `[i]`。

### 4.1 SignCompare / UsignCompare：有符号/无符号对拍

#### 4.1.1 概念说明

很多 DUT 的输出天然就是「带符号的定点数」或「无符号计数值」，在 testbench 里直接以 `signed` / `unsigned` 类型存在。如果每次比较都要先 `to_integer` 再调 `IntCompare`，既啰嗦又会在转换时把 32 位的坑提前踩到。

`SignCompare` 和 `UsignCompare` 解决的就是这个问题：它们直接吃 `signed` / `unsigned` 类型的期望值和实际值，让你少写一层转换。关键在于——

**比较动作本身使用的是 `numeric_std` 里定义的 `signed` / `unsigned` 关系运算符（`>=`、`<=`），这些运算符按操作数的完整声明位宽工作，不会截断到 32 位。** 也就是说，对一个 40 位的 `signed` 信号做 `SignCompare`，通过/不通过的判定是**正确**的。

那 32 位的限制从哪来？从**错误消息**里来：`SignCompare` 失败时用 `to_string` 打印期望/实际值，而 `to_string(signed)` 内部会先 `to_integer` 再 `integer'image`。`to_integer` 要把结果塞进 `integer` 类型，超范围就会出问题。所以「比较」和「消息」是两件相互独立的事，这一点是本讲最重要的认知。

#### 4.1.2 核心流程

`SignCompare` 与 `UsignCompare` 的执行过程：

1. 读取 `Expected`、`Actual`（都是 `signed` 或都是 `unsigned`）、容差 `Tolerance`、消息 `Msg`、前缀 `Prefix`。
2. 计算**容差带**通过条件：

\[
(\,Actual \ge Expected - Tolerance\,) \ \wedge\ (\,Actual \le Expected + Tolerance\,)
\]

   这里的 `>=`、`<=`、`-`、`+` 都是 `numeric_std` 对 `signed`（或 `unsigned`）定义的运算符，`Tolerance` 是 `integer`，`numeric_std` 提供了 `signed - integer`、`signed >= integer` 这类混合类型重载，按完整位宽求值。
3. 若条件为真，什么都不做（通过）。
4. 若条件为假，执行 `assert ... report ... severity error`，把消息拼成

\[
\text{Prefix} \oplus \text{Msg} \oplus \text{"[Expected "} \oplus to\_string(Expected) \oplus \text{", Received "} \oplus to\_string(Actual) \oplus \text{", Tolerance "} \oplus to\_string(Tolerance) \oplus \text{]"}
\]

   打到 Transcript。式中 \(\oplus\) 表示字符串拼接。

`SignCompare` 与 `UsignCompare` 在结构上完全对称，唯一区别是参数类型一个 `signed`、一个 `unsigned`。

#### 4.1.3 源码精读

先看声明。`SignCompare` 的接口（[hdl/psi_tb_compare_pkg.vhd:64-69](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L64-L69)）：

```vhdl
-- signed compare to signed
procedure SignCompare(Expected  : in signed;
                      Actual    : in signed;
                      Msg       : in string;
                      Tolerance : in integer := 0;
                      Prefix    : in string  := "###ERROR###: ");
```

`UsignCompare` 的接口几乎相同，只把类型换成 `unsigned`（[hdl/psi_tb_compare_pkg.vhd:78-83](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L78-L83)）。

再看 `SignCompare` 的实现（[hdl/psi_tb_compare_pkg.vhd:226-239](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L226-L239)）：

```vhdl
procedure SignCompare(...) is
begin
  assert (Actual >= Expected - Tolerance) and (Actual <= Expected + Tolerance)
  report Prefix & Msg & 
            " [Expected " & to_string(Expected) & 
            ", Received " & to_string(Actual) & 
            ", Tolerance " & to_string(Tolerance) & "]"
  severity error;
end procedure;
```

注意三个细节：

- **第 233 行的 assert 条件**：`Actual >= Expected - Tolerance`。`Actual`、`Expected` 是 `signed`，`Tolerance` 是 `integer`。这里走的是 `numeric_std` 的全位宽运算符，不经过 `integer`，所以 40 位比较的判定是正确的。
- **第 235–237 行的消息**：用 `to_string(Expected)`、`to_string(Actual)` 打印。这里的 `to_string` 是 `psi_tb_txt_util` 里**自造**的重载（不是 VHDL-2008 内建），它的实现决定了消息的位宽天花板。
- **第 237 行也用 `to_string(Tolerance)`**：`Tolerance` 本身就是 `integer`，所以它没有位宽问题。

那么 `to_string(signed)` 到底做了什么？看底座文本包（[hdl/psi_tb_txt_util.vhd:367-370](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L367-L370)）：

```vhdl
function to_string(num : signed) return string is
begin
  return integer'image(to_integer(num));
end function;
```

它先 `to_integer(num)` 把 `signed` 转成 `integer`，再 `integer'image` 转成十进制字符串。`unsigned` 版本（[hdl/psi_tb_txt_util.vhd:372-375](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L372-L375)）同理。

`to_integer` 是关键：它要返回 `integer`，而 `integer` 只有 32 位。所以一旦 `num` 表示的数值超出 \([-2^{31},\ 2^{31}-1]\)，`to_integer` 就会在多数仿真器里触发运行期错误（如 range check failure）或返回无意义的数值——**这就是 `SignCompare` 错误消息的 32 位天花板**。注意：只有当数值真的超出 32 位范围时才会出问题；如果你的 40 位信号实际取值都落在 32 位范围内，`SignCompare` 的消息仍然正常显示十进制。

`UsignCompare` 的实现（[hdl/psi_tb_compare_pkg.vhd:256-269](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L256-L269)）和 `SignCompare` 逐行对称，只是类型是 `unsigned`，不再赘述。

#### 4.1.4 代码实践

**目标**：确认「比较判定」与「错误消息」是两件事。

**步骤**：

1. 打开 [hdl/psi_tb_compare_pkg.vhd:226-239](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L226-L239) 和 [hdl/psi_tb_txt_util.vhd:367-370](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L367-L370)。
2. 假设有一个 40 位 `signed` 信号，实际值是 \(+2^{31}\)（即 2_147_483_648，刚好超出 32 位有符号上界 \(2^{31}-1\)）。
3. 先在脑子里推演：`SignCompare` 第 233 行的 assert 用 `signed` 运算符比较，能否正确判定 \(+2^{31} \ge +2^{31}\)？答案应该是「能，判定为通过」。
4. 再推演：如果期望值故意写错成 \(+2^{31}-1\)，assert 判定为不通过，于是去拼消息，调用 `to_string(Expected)` → `to_integer(signed)`。此时 \(+2^{31}\) 超出 `integer` 范围，会发生什么？

**需要观察的现象**：assert 的判定逻辑（第 233 行）与消息拼接（第 235–237 行）走的是两条完全不同的代码路径；前者用 `numeric_std` 全位宽运算符，后者用 `to_integer`。

**预期结果**：比较判定在任何位宽下都正确；消息打印在数值超出 32 位范围时会出问题。具体的仿真器报错形式（range check failure / 包裹值 / 仅警告）**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`SignCompare` 和 `UsignCompare` 的实现几乎一模一样，为什么不写成一个通用过程？

**答案**：因为 `signed` 和 `unsigned` 是 `numeric_std` 里两个**不同的类型**，VHDL 要求过程按类型分别定义（或重载）。同一个比特串，`signed` 按补码解释、`unsigned` 按无符号解释，关系运算符也是分别重载的，所以必须各写一份。

**练习 2**：`SignCompare` 的 assert 条件里，`Expected - Tolerance` 是什么类型？为什么 40 位时它不会出错？

**答案**：`Expected` 是 `signed`，`Tolerance` 是 `integer`，`numeric_std` 提供了 `signed - integer` 的重载，结果是 `signed`，按 `Expected` 的完整位宽求值。接着 `Actual >= (signed)` 也是 `numeric_std` 的 `signed` 关系运算符，全位宽工作，所以 40 位时判定正确。

---

### 4.2 SignCompare2：用十六进制消息突破 32 位限制

#### 4.2.1 概念说明

`SignCompare2` 专门为「宽位有符号信号」而生。它的比较逻辑和 `SignCompare` **完全相同**（assert 条件一字不差），唯一改动是错误消息：不再用会触发 `to_integer` 的 `to_string`，而是用 `hstr(std_logic_vector(...))` 直接把整段比特按每 4 位一个十六进制字符打印出来。

为什么 `hstr` 能突破 32 位？因为 `hstr` 根本不经过 `integer`：它把 `std_logic_vector` 每 4 个比特（一个 nibble）查表映射到一个十六进制字符（`0`–`9`、`A`–`F`），与数值大小无关，只与比特模式有关。所以无论 32 位、40 位还是 64 位，它都能正确显示原始比特的十六进制。

代价是：十六进制对人类不如十进制直观，尤其负数要以补码形式呈现（例如 40 位 `signed` 的 \(-1\) 会显示成 `0xFFFFFFFFFF`），读者需要自己换算符号位。这就是 `SignCompare` 与 `SignCompare2` 的取舍：可读性 vs 位宽兼容性。

#### 4.2.2 核心流程

`SignCompare2` 的执行过程：

1. 读取 `Expected`、`Actual`（都是 `signed`）、`Tolerance`、`Msg`、`Prefix`。
2. 计算**与 `SignCompare` 完全相同**的容差带条件：

\[
(\,Actual \ge Expected - Tolerance\,) \ \wedge\ (\,Actual \le Expected + Tolerance\,)
\]

3. 若为真，什么都不做。
4. 若为假，拼消息时**不再**调用 `to_string(Expected)`，而是：

\[
\text{Prefix} \oplus \text{Msg} \oplus \text{"[Expected 0x"} \oplus hstr(std\_logic\_vector(Expected)) \oplus \text{", Received 0x"} \oplus hstr(std\_logic\_vector(Actual)) \oplus \text{", Tolerance "} \oplus to\_string(Tolerance) \oplus \text{]"}
\]

   注意 `signed` 先用 `std_logic_vector(...)` 转回裸比特视图，再交给 `hstr`。`Tolerance` 仍是 `integer`，用 `to_string` 安全。

#### 4.2.3 源码精读

声明里有一行注释直接点明了它的用途（[hdl/psi_tb_compare_pkg.vhd:71-76](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L71-L76)）：

```vhdl
-- signed compare to signed (output message is hex string to handle data width > 32)
procedure SignCompare2(Expected  : in signed;
                       Actual    : in signed;
                       Msg       : in string;
                       Tolerance : in integer := 0;
                       Prefix    : in string  := "###ERROR###: ");
```

实现（[hdl/psi_tb_compare_pkg.vhd:241-254](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L241-L254)）：

```vhdl
procedure SignCompare2(...) is
begin
  assert (Actual >= Expected - Tolerance) and (Actual <= Expected + Tolerance)
  report Prefix & Msg & 
          " [Expected 0x" & hstr(std_logic_vector(Expected)) & 
          ", Received 0x" & hstr(std_logic_vector(Actual)) & 
          ", Tolerance " & to_string(Tolerance) & "]"
  severity error;
end procedure;
```

对比 4.1.3 里 `SignCompare` 的实现，可以看到：

- **第 248 行的 assert 条件**与 `SignCompare` 的第 233 行**逐字符相同**——这是「比较逻辑不变」的硬证据。
- **第 250–251 行**用 `hstr(std_logic_vector(Expected))` 取代了 `to_string(Expected)`，这就是「消息换 hex」的唯一改动。

再确认 `hstr` 为何安全。看 [hdl/psi_tb_txt_util.vhd:312-349](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L312-L349)（节选关键部分）：

```vhdl
function hstr(slv: std_logic_vector) return string is
  variable longslv : std_logic_vector(67 downto 0):=(others => '0');
  ...
begin
  hexlen:=(slv'left+1)/4;
  ...
  longslv(slv'left downto 0) := slv;
  for i in (hexlen-1) downto 0 loop
      fourbit:=longslv(((i*4)+3) downto (i*4));
      case fourbit is
           when "0000" => hex(...):='0';
           ...
           when "1111" => hex(...):='F';
           when others => hex(...):='?';
      end case;
  end loop;
  return hex(1 to hexlen);
end function;
```

它每次取 4 个比特（`fourbit`）查表输出一个字符，全程没有 `to_integer`、没有 `integer`，所以位宽不被 32 限制（实现里用了一个 68 位的内部缓冲 `longslv` 来对齐比特，覆盖了常见的 32/40/64 位场景）。

#### 4.2.4 代码实践

**目标**：对比同一个失败用例下 `SignCompare` 与 `SignCompare2` 的消息差异。

**步骤**：

1. 让一个 40 位 `signed` 信号持有期望值 \(+2^{31}\)（超出 32 位有符号范围），并故意让实际值与期望值不同，触发失败消息。
2. 先调用 `SignCompare`，再调用 `SignCompare2`，观察两者输出。
3. 注意：\(+2^{31}\) 不能用 `integer` 字面量直接写（它本身就溢出 `integer`），所以要用十六进制字面量构造，例如 `signed(X"0080000000")`（40 位 = 10 个十六进制位，最高位为 0 表示正数）。

**需要观察的现象**：`SignCompare2` 应当稳定输出形如 `[Expected 0x0080000000, Received 0x..., Tolerance 0]` 的消息；而 `SignCompare` 在这条路径上会调用 `to_integer` 处理超出 32 位范围的值。

**预期结果**：`SignCompare2` 的消息在任何位宽下都干净可读；`SignCompare` 的消息在该超范围场景下的具体表现**待本地验证**（不同仿真器对 `to_integer` 超范围的处理不同）。完整的可运行 testbench 见第 5 节综合实践。

#### 4.2.5 小练习与答案

**练习 1**：既然 `SignCompare2` 更通用，为什么不直接删掉 `SignCompare`？

**答案**：因为十进制消息对人更友好。当数据实际落在 32 位范围内时（这在很多计数器、小范围定点数场景里是常态），`SignCompare` 的 `[Expected 1234, Received 1235, Tolerance 0]` 比 `SignCompare2` 的 `[Expected 0x000004D2, ...]` 直观得多。两者是「可读性」与「位宽兼容性」的权衡，按数据实际宽度选择即可。

**练习 2**：用 `SignCompare2` 比较一个值为 \(-1\) 的 40 位 `signed` 信号，失败消息里 `Received 0x...` 会显示成什么？为什么？

**答案**：显示成 `0xFFFFFFFFFF`（10 个 F）。因为 \(-1\) 的 40 位补码是全 1，`hstr` 直接按比特映射成十六进制，不做符号解释。这正是 hex 消息的「不直观」之处——你需要自己看最高位（这里是 F，即比特为 1）判断它是负数。

---

### 4.3 SignCompareInt / UsignCompareInt：与整数期望值比较（复用 StdlvCompareInt）

#### 4.3.1 概念说明

在很多 testbench 里，期望值就是一个普通的整数常量或字面量（例如「期望读到 5」），而 DUT 输出是 `signed` / `unsigned` 信号。如果每次都手动 `std_logic_vector(Actual)` 再调 `StdlvCompareInt(..., IsSigned => true/false, ...)`，代码会很啰嗦，而且容易把 `IsSigned` 写反。

`SignCompareInt` 和 `UsignCompareInt` 就是这两个调用的「语法糖」：它们帮你把 `signed` / `unsigned` 实际值转成 `std_logic_vector`，并自动设好 `IsSigned` 参数，然后直接转交给 `StdlvCompareInt`。

但这同时意味着——**它们完整继承了 `StdlvCompareInt` 的 32 位限制**。因为 `StdlvCompareInt` 内部会用 `to_integer` 把实际值转成 `integer` 再比较（见 u3-l1），所以 `SignCompareInt` / `UsignCompareInt` 不仅消息受 32 位限制，连**比较判定本身**也被限制在 32 位。这是它们和 `SignCompare` / `SignCompare2` 最本质的区别：

- `SignCompare` / `SignCompare2`：比较判定全位宽正确，只有（`SignCompare` 的）消息可能受 32 位影响。
- `SignCompareInt` / `UsignCompareInt`：比较判定和消息都受 32 位限制（因为转交给了 `StdlvCompareInt`）。

所以对超过 32 位的宽信号，应当用 `SignCompare2`，而不是 `SignCompareInt`。

#### 4.3.2 核心流程

`SignCompareInt` / `UsignCompareInt` 的执行过程：

1. 读取 `Expected`（`integer`）、`Actual`（`signed` / `unsigned`）、`Tolerance`、`Msg`、`Prefix`。
2. 把 `Actual` 转成 `std_logic_vector`：
   - `SignCompareInt` 调用 `StdlvCompareInt(..., IsSigned => true, ...)`；
   - `UsignCompareInt` 调用 `StdlvCompareInt(..., IsSigned => false, ...)`。
3. 之后完全进入 `StdlvCompareInt` 的流程（见 u3-l1）：它用 `to_integer(signed(unsigned(Actual)))` 把实际值变成 `integer`，按容差带 \([E-T, E+T]\) 比较 `integer`，失败时打印 `[Expected <int>(0x<32bit hex>), Received <int>(0x<32bit hex>), Tolerance <int>]`。

#### 4.3.3 源码精读

声明（[hdl/psi_tb_compare_pkg.vhd:85-90](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L85-L90) 与 [hdl/psi_tb_compare_pkg.vhd:92-97](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L92-L97)）：

```vhdl
-- signed compare to integer
procedure SignCompareInt(Expected  : in integer;
                         Actual    : in signed;
                         Msg       : in string;
                         Tolerance : in integer := 0;
                         Prefix    : in string  := "###ERROR###: ");

-- unsigned compare to integer
procedure UsignCompareInt(Expected  : in integer;
                          Actual    : in unsigned;
                          Msg       : in string;
                          Tolerance : in integer := 0;
                          Prefix    : in string  := "###ERROR###: ");
```

`SignCompareInt` 实现（[hdl/psi_tb_compare_pkg.vhd:271-284](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L271-L284)）：

```vhdl
procedure SignCompareInt(...) is
begin
  StdlvCompareInt(Expected  => Expected,
                  Actual    => std_logic_vector(Actual),
                  Msg       => Msg,
                  IsSigned  => true,
                  Tolerance => Tolerance,
                  Prefix    => Prefix);
end procedure;
```

`UsignCompareInt` 实现（[hdl/psi_tb_compare_pkg.vhd:287-299](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L287-L299)）：

```vhdl
procedure UsignCompareInt(...) is
begin
  StdlvCompareInt(Expected  => Expected,
                  Actual    => std_logic_vector(Actual),
                  Msg       => Msg,
                  IsSigned  => false,
                  Tolerance => Tolerance,
                  Prefix    => Prefix);
end procedure;
```

这两个过程是典型的「包装器（wrapper）」模式：除了 `std_logic_vector(Actual)` 这一次视图转换和 `IsSigned` 取值的差异，其余参数原样透传。所有真正的逻辑都在 `StdlvCompareInt` 里（[hdl/psi_tb_compare_pkg.vhd:112-140](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L112-L140)，u3-l1 已精读）。

注意 `IsSigned` 的取值约定（来自 `StdlvCompareInt`，u3-l1 讲过）：
- `IsSigned => true`：`to_integer(signed(Actual))`，按二进制补码解释，适合 `SignCompareInt`。
- `IsSigned => false`：`to_integer(unsigned(Actual))`，按无符号解释，适合 `UsignCompareInt`。

选错会把同一个比特串解释成完全不同的数值，导致误报。

#### 4.3.4 代码实践

**目标**：理解 `SignCompareInt` / `UsignCompareInt` 是 `StdlvCompareInt` 的薄包装。

**步骤**：

1. 对照 [hdl/psi_tb_compare_pkg.vhd:271-284](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L271-L284)（`SignCompareInt`）和 [hdl/psi_tb_compare_pkg.vhd:287-299](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L287-L299)（`UsignCompareInt`），数一数两者函数体的差异。
2. 你应当发现：唯一的区别就是 `IsSigned => true` vs `IsSigned => false`。
3. 再翻到 `StdlvCompareInt` 的实现 [hdl/psi_tb_compare_pkg.vhd:112-140](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L112-L140)，确认它内部确实用 `to_integer` 比较 `integer`，并且消息里的十六进制来自 `std_logic_vector(31 downto 0)` 的临时变量。

**需要观察的现象**：这两个过程的函数体里没有任何 `assert`、没有任何消息拼接——它们只是「转交」。

**预期结果**：你会清楚地看到，`SignCompareInt` / `UsignCompareInt` 的全部行为（比较、消息、32 位限制）都由 `StdlvCompareInt` 决定。

#### 4.3.5 小练习与答案

**练习 1**：一个 40 位 `signed` 信号，实际值是 \(+2^{31}\)。用 `SignCompareInt(2147483648, Actual, "x")` 去比，会发生什么？

**答案**：这段代码根本编译不过——`2147483648` 超出了 `integer` 的上界 \(2^{31}-1 = 2147483647\)，不能写成 `integer` 字面量。退一步，即便用合法的整数期望值，只要 `Actual` 转出来的数值超出 32 位范围，`StdlvCompareInt` 内部的 `to_integer` 就会出问题。这正是宽信号要用 `SignCompare2` 而非 `SignCompareInt` 的原因。

**练习 2**：为什么不把 `SignCompareInt` / `UsignCompareInt` 合并成一个带 `IsSigned` 参数的过程？

**答案**：可以合并，但 psi_tb 的设计选择是「按类型分过程」而不是「按布尔参数分模式」，这样调用点 `SignCompareInt(5, sig, "x")` / `UsignCompareInt(5, usig, "x")` 由参数类型自动决定解释方式，调用者不必每次记得传 `IsSigned`，更不容易写反。这与 4.1 里 `SignCompare` / `UsignCompare` 分开是同一套设计思路。

---

### 4.4 IndexString：在循环里给每条比较打上下标标签

#### 4.4.1 概念说明

批量检查时，我们经常在 `for` 循环里反复调用比较过程（例如遍历一个期望值数组，逐项与 DUT 输出比对）。如果第 37 项不匹配，失败消息最好能直接告诉你「是第 37 项挂了」，而不是让你对着一大堆相同的消息猜。

`IndexString` 就是一个极小的辅助函数：它把一个整数下标格式化成 `[i]` 形式的字符串，方便你拼进 `Msg` 参数里。例如 `IndexString(37)` 返回 `"[37]"`，于是消息可以写成 `"Element " & IndexString(i) & " mismatch"`，失败时显示 `###ERROR###: Element [37] mismatch [...]`。

#### 4.4.2 核心流程

`IndexString` 的执行过程：

1. 读取整数 `Index`。
2. 用 `to_string(Index)`（`psi_tb_txt_util` 里的 `integer` 重载，等价于十进制字符串）把数字转成字符串。
3. 在前后各拼接一个方括号字符，返回 `"[<十进制字符串>]"`。

即：

\[
\text{IndexString}(i) = \text{"["} \oplus to\_string(i) \oplus \text{]"}
\]

#### 4.4.3 源码精读

声明带一行很贴心的注释（[hdl/psi_tb_compare_pkg.vhd:22-23](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L22-L23)）：

```vhdl
-- returns an index string in the form "[3]"
function IndexString(Index : integer) return string;
```

实现（[hdl/psi_tb_compare_pkg.vhd:106-110](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L106-L110)）：

```vhdl
function IndexString(Index : integer) return string is
begin
  return "[" & to_string(Index) & "]";
end function;
```

短短三行。这里的 `to_string(Index)` 走的是 `psi_tb_txt_util` 里 `integer` 的重载（[hdl/psi_tb_txt_util.vhd:357-360](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L357-L360)），它内部又调用 `str(int)`，按十进制输出，支持负数（会带前导 `-`）。

典型的循环用法（示例代码，非项目原有）：

```vhdl
for i in 0 to 15 loop
  SignCompareInt(ExpArr(i), DataArr(i),
                 "Tap " & IndexString(i) & " ");
end loop;
```

当 `i = 7` 这一项不匹配时，Transcript 里会出现类似：

```
###ERROR###: Tap [7]  [Expected 12(0x0000000C), Received 13(0x0000000D), Tolerance 0]
```

一眼就能定位到第 7 项。

#### 4.4.4 代码实践

**目标**：体会 `IndexString` 在循环里的可读性收益。

**步骤**：

1. 想象一个 `for i in 0 to 3 loop`，循环体里调用某个比较过程，故意让 `i = 2` 那一项不匹配。
2. 写两种 `Msg`：一种不带下标，例如 `"mismatch"`；另一种带下标，例如 `"Element " & IndexString(i) & " mismatch"`。
3. 推演失败时 Transcript 里分别会显示什么。

**需要观察的现象**：带 `IndexString` 的消息能直接告诉你「第几项」失败，不带的则一片雷同。

**预期结果**：带下标的失败消息形如 `###ERROR###: Element [2] mismatch [...]`，定位明确。可运行版本见第 5 节综合实践。

#### 4.4.5 小练习与答案

**练习 1**：`IndexString(-1)` 返回什么？

**答案**：返回 `"[-1]"`。因为 `to_string(Index)` 对负数会输出带前导 `-` 的十进制字符串，所以下标是负数也能正确显示（虽然实际用法里下标通常非负）。

**练习 2**：如果不用 `IndexString`，直接写 `"Element [" & to_string(i) & "]"` 效果一样吗？为什么还要提供这个函数？

**答案**：效果完全一样，`IndexString` 就是这一行的封装。提供它的意义在于「命名即意图」：循环里看到 `IndexString(i)` 立刻知道这是在「打下标标签」，比裸拼接更易读，也减少手写时漏掉方括号或拼错的概率。

---

## 5. 综合实践

把本讲四个模块串起来：写一个最小 testbench，对一个 40 位 `signed` 信号分别用 `SignCompare`、`SignCompare2`、`SignCompareInt` 做期望值比较，并在一个 `for` 循环里用 `IndexString` 给每项打下标。重点对比 `SignCompare` 与 `SignCompare2` 在「数值超出 32 位范围」时的消息差异。

下面的代码是**示例代码**（非项目原有文件），演示用法。它依赖 `psi_tb_txt_util` 和 `psi_tb_compare_pkg` 两个包，编译顺序遵循 u1-l2 讲过的拓扑（txt_util → compare_pkg → 本 TB）。

```vhdl
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_compare_pkg.all;

-- 示例代码：演示 SignCompare / SignCompare2 / SignCompareInt 与 IndexString
entity compare_signed_demo_tb is
end entity;

architecture sim of compare_signed_demo_tb is
  signal Data_s : signed(39 downto 0) := (others => '0');
begin
  process
    -- 落在 32 位有符号范围内：1_000_000
    constant InRange_c  : signed(39 downto 0) := to_signed(1_000_000, 40);
    -- 超出 32 位有符号范围：+2^31 = 2_147_483_648 > 2^31-1
    -- 不能用 integer 字面量（它本身溢出 integer），必须用十六进制构造
    -- 40 位 = 10 个十六进制位，X"0080000000" 的 bit31=1，最高位 bit39=0 => 正数 +2^31
    constant OutRange_c : signed(39 downto 0) := signed(X"0080000000");
  begin
    -- 场景 1：范围内，两个函数都应通过、消息都正常
    Data_s <= InRange_c;
    wait for 10 ns;
    print(">>> 场景 1：范围内，应通过");
    SignCompare (InRange_c, Data_s, "S1-SignCompare ");
    SignCompare2(InRange_c, Data_s, "S1-SignCompare2 ");

    -- 场景 2：超范围（实际=期望，应通过；但若有不匹配，观察消息差异）
    Data_s <= OutRange_c;
    wait for 10 ns;
    print(">>> 场景 2：超范围，应通过");
    SignCompare (OutRange_c, Data_s, "S2-SignCompare ");   -- to_string -> to_integer 超范围
    SignCompare2(OutRange_c, Data_s, "S2-SignCompare2 ");  -- hstr 直接输出 hex，安全

    -- 场景 3：故意不匹配，观察 SignCompare2 的 hex 消息格式
    Data_s <= to_signed(123, 40);
    wait for 10 ns;
    print(">>> 场景 3：故意不匹配，观察 hex 消息");
    SignCompare2(to_signed(456, 40), Data_s, "S3-fail ");

    -- 场景 4：循环里用 IndexString 给每项打下标，并演示 SignCompareInt 复用 StdlvCompareInt
    for i in 0 to 3 loop
      Data_s <= to_signed(i * 1000, 40);
      wait for 1 ns;
      SignCompareInt(i * 1000, Data_s,
                     "Element " & IndexString(i) & " ");
    end loop;

    print("=== compare_signed_demo DONE ===");
    wait;
  end process;
end architecture;
```

**操作步骤**：

1. 把上面的 TB 保存为一个 `.vhd` 文件（例如 `compare_signed_demo_tb.vhd`），与 psi_tb 源码放在能被同一 `work` 库编译的位置（参考 u1-l2 讲过的 `sim/config.tcl` 编译分组）。
2. 参考 u1-l3 的流程，用 PsiSim 编译：先 `psi_tb_txt_util`，再 `psi_tb_compare_pkg`，最后本 TB。
3. 运行仿真，查看 Transcript。

**需要观察的现象**：

- **场景 1**：`SignCompare` 与 `SignCompare2` 都通过（无 `###ERROR###`）。
- **场景 2**：两者都应通过（实际等于期望）。重点在于——如果此时把期望值改错触发失败，`SignCompare2` 会稳定打印 `[Expected 0x0080000000, Received 0x..., Tolerance 0]`；而 `SignCompare` 在 `to_integer` 处理 \(+2^{31}\) 时的具体表现因仿真器而异。
- **场景 3**：会打印一条失败消息，形如 `###ERROR###: S3-fail  [Expected 0x00000001C8, Received 0x000000007B, Tolerance 0]`（`456 = 0x1C8`，`123 = 0x7B`，40 位补齐前导零）。
- **场景 4**：四项都通过；如果你把 `i * 1000` 故意改成 `i * 1000 + 1`，会看到带下标的失败消息 `###ERROR###: Element [0] [...]` … `Element [3] [...]`，演示 `IndexString` 的定位作用。

**预期结果 / 待本地验证**：场景 1、3、4 的行为可由源码确定地推演；场景 2 中 `SignCompare` 处理超 32 位数值时的报错形式（range check failure / 包裹值 / 仅警告）依赖具体仿真器，**待本地验证**。无论 `SignCompare` 表现如何，`SignCompare2` 在所有场景下都应给出干净的十六进制消息——这就是它存在的意义。

## 6. 本讲小结

- `SignCompare` / `UsignCompare` 直接吃 `signed` / `unsigned` 类型的期望值与实际值；**比较判定走 `numeric_std` 全位宽运算符，是正确的**，只有 `SignCompare` 的错误消息经 `to_string → to_integer` 受 32 位整数限制。
- `SignCompare2` 与 `SignCompare` 的 assert 条件**逐字符相同**，唯一区别是消息用 `hstr(std_logic_vector(...))` 直接按 nibble 映射十六进制，不经 `integer`，所以能正确显示任意位宽——代价是对人不如十进制直观。
- `SignCompareInt` / `UsignCompareInt` 只是 `StdlvCompareInt`（`IsSigned => true/false`）的薄包装，**比较和消息都继承了 32 位限制**；对宽信号应优先选 `SignCompare2`。
- 「比较判定」和「错误消息」是两条独立的代码路径：前者可以全位宽正确，后者却可能被 32 位整数拖累——这是本讲最重要的认知。
- `IndexString(i)` 返回 `"[i]"`，在 `for` 循环里拼进 `Msg` 即可让批量检查的失败消息一眼定位到具体下标。
- 所有过程沿用统一前缀 `"###ERROR###: "` 与 `severity error`（只打印不中断），失败即被 CI 的 `run_check_errors "###ERROR###"` 捕获。

## 7. 下一步学习建议

本讲讲完了 `psi_tb_compare_pkg` 的全部比较过程，它们是后续所有 BFM 的公共检查底座。接下来可以：

- 进入 **u4（psi_tb_activity_pkg）**：你会看到 `CheckNoActivity` / `CheckLastActivity` 等过程内部如何复用本讲的 `StdlCompare` / `StdlvCompareInt`，把「信号在一段时间内是否翻转」也变成带 `###ERROR###` 的可读检查。
- 或者进入 **u5（psi_tb_axi_pkg）**：AXI BFM 在响应错误时会复用 `StdlvCompareStdlv` 报错，届时你会再次看到本讲建立的「统一前缀 + assert report」模式。
- 想巩固本讲，可以把第 5 节的 testbench 真正在 ModelSim 或 GHDL 里跑一遍（参考 u1-l3 的 `run.tcl` / `runGhdl.tcl`），亲眼看 `SignCompare` 与 `SignCompare2` 在场景 2 的输出差异——这是理解「消息位宽天花板」最直接的方式。
