# 日志解析与源码行定位

## 1. 本讲目标

上一讲（u5-l1）我们搞清楚了 npu check 的错误体系：它寄生在 `RunKernelFunctionOnCpu` 的 fork 模型上，在算子执行时全程跟踪内存与同步，把违例以扁平错误码（`ErrorRead3`、`ErrorWrite1`……）写进 `*_npuchk.log`。但 `*_npuchk.log` 是给机器看的——里面是十六进制地址和 C++ 修饰（mangled）符号，人类难以直接读懂。

本讲只解决一个问题：**`ascendc_npuchk_report.py` 这个脚本如何把一份机器向的 `*_npuchk.log`，翻译成人能看懂的「错误类型 + 规则解释 + 内建函数签名 + 源码函数与行号」报告。**

学完后你应该能够：

- 说清 `*_npuchk.log` 的文本结构，以及 `parse_log` 用什么「状态机」逐行把它拆开。
- 理解 BackTrace 堆栈是如何被提取、去重、并组装成 key 的。
- 理解 `addr2line` 与 `c++filt` 这两个 binutils 工具如何把二进制地址映射回源码函数与行号，并能独立执行这条命令链验证脚本结果。

## 2. 前置知识

### 2.1 从「地址」到「源码行」需要什么

CPU 域算子本质上是一个用 GCC 编译出的 ELF 可执行文件（例如 u2 样例里的 `./add`）。算子在 CPU 上跑出错时，npuchk 记录到日志里的「地址」是程序运行时的指令地址（形如 `0x4a3f`）。光看这个数字，你完全不知道它对应哪一行源码。

要把地址翻译回源码，需要两样东西：

1. **带调试符号的二进制**：编译时必须带 `-g`，这样 ELF 里会嵌入 DWARF 调试段，记录「每条指令地址 ↔ 源码文件:行号 ↔ 所属函数」的映射。
2. **一个查表的工具**：GNU binutils 提供的 `addr2line`，输入地址，输出函数名与 `文件:行号`。

### 2.2 C++ 的 name mangling 与 c++filt

C++ 支持函数重载，编译器为了在符号表里区分 `Add(int)` 和 `Add(float)`，会把参数类型编码进符号名，这叫 **name mangling**（名称修饰）。修饰后的符号长这样：

```
_ZN8KernelAdd7ProcessEv
```

人类根本读不懂。`c++filt` 是 binutils 的另一个工具，专门做「反修饰」，把上面这串还原成：

```
KernelAdd::Process()
```

所以完整的地址翻译链是：**地址 → `addr2line`（拿到修饰的函数名 + 文件:行号）→ `c++filt`（把函数名反修饰成可读形式）**。本讲的 `addr_to_line` 函数就是把这两步串起来的。

### 2.3 与上一讲的衔接

上一讲我们知道 `*_npuchk.log` 由闭源的 `libcpudebug_npuchk.so` 在运行时生成，**本讲只关心这份日志的「读取与解析」**，不再涉及它的生成机制。解析全部由开源的、不到 180 行的 [npuchk/ascendc_npuchk_report.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py) 完成，这正是本讲的全部研究对象。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [npuchk/ascendc_npuchk_report.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py) | 唯一核心文件。包含日志解析、堆栈提取、addr2line 调用与报告生成全部逻辑 |
| [docs/02_npu_check.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md) | npu check 使用文档，给出脚本调用方式、错误类型表与输出样例 |

`ascendc_npuchk_report.py` 由五个函数组成，分工如下：

| 函数 | 行号 | 职责 |
|------|------|------|
| `get_error_type` | 23–31 | 从一行文本里提取 `[ErrorXxx]` 错误码 |
| `parse_log` | 34–71 | 逐行扫描日志，状态机式提取「错误行 + 内建函数签名 + 堆栈地址」 |
| `execute_cmd` | 74–87 | 封装 `subprocess`，带 10 秒超时执行外部命令 |
| `addr_to_line` | 90–100 | 调 `addr2line` + `c++filt`，地址→函数与行号 |
| `__main__` 块 | 103–179 | 输入分发、错误统计、报告拼装与打印 |

## 4. 核心概念与源码讲解

### 4.1 npuchk.log 文本结构与 parse_log 状态机

#### 4.1.1 概念说明

`parse_log` 要解决的问题是：`*_npuchk.log` 是一份**半结构化文本**——它不是 JSON，也不是表格，而是按固定标记（marker）分节的纯文本。脚本必须靠识别这些标记（`### `、`[Error`、`# BackTrace #`）逐行切分，才能把每一条错误连同它的上下文（触发错误的内建函数、调用堆栈）打包成一条记录。

这种「用若干布尔标志记录当前处于文本的哪一节、据此决定每行如何处理」的写法，本质上是一个**手写的文本状态机**。

#### 4.1.2 核心流程

根据 `parse_log` 的解析逻辑，可以反推出 `*_npuchk.log` 一条错误记录的文本结构（仓库未随附样例日志，下图为依据解析代码与 [docs/02_npu_check.md:23-32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L23-L32) 的输出样例**还原的示意结构**，标注为「示意」）：

```
### vadd((__ubuf__ half*)0x7f328c11b810, ...)        ← 标记 A：内建函数签名行（以 "### " 开头）
[V] [ErrorRead3] on read 0x7f328c11b010 0x800B       ← 标记 B：错误行（含 "[Error"）
 # BackTrace #                                       ← 标记 C：堆栈段开始
  ./add(+0x4a3f)                                     ← 标记 D：堆栈帧（以两个空格开头，形如 bin(+addr)）
  ./add(+0x3b2c)
  /usr/lib/libfoo.so(+0x1c8d)                        ← 含 ".so" 的帧会被丢弃
                                                      ← 标记 E：堆栈段结束（非缩进行）
```

`parse_log` 的状态机可用下面的伪代码描述：

```
初始化: cce_intri = ""          # 最近一次见到的内建函数签名
        bs_start = False        # 是否处于 BackTrace 段内
        err_info = []           # 当前错误的上下文累积列表
        key = ""                # 当前错误的去重键

对日志的每一行 line:
    若 line 含 "[Error":                       # 命中错误行
        err_info ← [line, cce_intri]            # 记下错误行 + 触发它的内建函数签名
        key ← 错误码                            # 去重键以错误码开头
        继续
    若 line 以 "### " 开头:                     # 命中内建函数签名行
        cce_intri ← line                        # 暂存，留给下一条错误用
        继续
    若 (未在堆栈段) 且 line 含 "# BackTrace #": # 命中堆栈段开始
        bs_start ← True
        继续
    若 (在堆栈段) 且 line 不以两空格开头:        # 命中堆栈段结束
        bs_start ← False
        若 key 未出现过: stack[key] ← err_info  # 提交本条记录
        清空 err_info、key
        继续
    若 在堆栈段:                                # 命中堆栈帧
        若 line 含 ".so": 跳过                  # 丢弃库帧
        解析出 binfile 与 addr
        err_info ← binfile:addr
        key ← addr                             # 去重键追加每个地址
```

几个关键设计点先在这里点出，源码精读里再展开：

- **内建函数签名先于错误行出现**：`### ` 行被暂存到 `cce_intri`，等到下一行 `[Error...]` 出现时才被「消费」进 `err_info`。所以日志里 `### vadd(...)` 总在 `[ErrorRead3]` 之前。
- **去重键 = 错误码 + 全部堆栈地址**：见 4.2。
- **库帧（`.so`）直接丢弃**：堆栈里属于 `.so` 的帧（如 libc、闭源模型库）不参与源码定位。

#### 4.1.3 源码精读

先看错误码提取函数 [get_error_type](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L23-L31)。它的逻辑是「找 `[Error`，再找配对的 `]`，取中间文本」：

```python
def get_error_type(info_input):
    err_start = info_input.find("[Error")
    if err_start < 0:
        return None
    err_info_str = info_input[err_start + 1 :]   # 从 "Error..." 开始切
    err_stop = err_info_str.find("]")
    if err_stop < 0:
        return None
    return err_info_str[:err_stop]               # 例如 "ErrorRead3"
```

注意一个细节：`err_start + 1` 跳过了 `[`，所以返回的是 `ErrorRead3` 而不是 `[ErrorRead3`。这个函数同时承担「判断某行是不是错误行」（返回 `None` 即不是）和「提取错误码」两个职责，是状态机的核心探测器。

再看 [parse_log](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L34-L71) 的前半段，即「错误行 + 内建函数签名」的捕获：

```python
for line in lines:
    err_type_line = get_error_type(line)
    if err_type_line is not None:          # 命中错误行
        err_info.append(line.strip())
        err_info.append(cce_intri)         # 消费上一行暂存的内建函数签名
        key += err_type_line               # 去重键 = 错误码
        continue
    if line.startswith("### "):            # 命中内建函数签名行，暂存
        cce_intri = line.strip()
        continue
```

这里 `err_info` 在命中错误行时被「重建」为 `[错误行, 内建函数签名]` 两个元素，后续堆栈帧会追加在它后面。注意 `err_info.append(line.strip())` 并没有先清空列表——因为每次进入新的错误行时，前一条记录已经在堆栈段结束时（4.2 会看到）被提交并清空过了。

> **小结**：`parse_log` 用 `cce_intri`、`bs_start` 两个变量加上 `get_error_type` 的探测，把半结构化文本切成一条条 `(错误行, 内建函数签名, [堆栈帧...])` 的记录。

#### 4.1.4 代码实践

**实践目标**：用一段自己造的「迷你日志」喂给 `get_error_type`，验证错误码提取正确，从而在不依赖真实 npuchk 运行的前提下理解状态机。

**操作步骤**：

1. 在仓库根目录进入 Python 交互环境，把脚本的解析函数导入：

   ```bash
   cd asc-tools
   python3 -c "import importlib.util; \
   spec = importlib.util.spec_from_file_location('r', 'npuchk/ascendc_npuchk_report.py'); \
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); \
   print(m.get_error_type('[V] [ErrorRead3] on read 0x7f328c11b010 0x800B')); \
   print(m.get_error_type('this line has no error')); \
   print(m.get_error_type('[ErrorWrite1] write to freed buffer'))"
   ```

   > 说明：因为文件名含下划线、且我们只想复用函数而不触发 `__main__`，这里用 `importlib` 按文件路径加载模块。

**需要观察的现象**：第一行打印 `ErrorRead3`；第二行打印 `None`；第三行打印 `ErrorWrite1`。

**预期结果**：三行输出依次为 `ErrorRead3` / `None` / `ErrorWrite1`。若如此，说明你已经理解 `get_error_type` 是状态机的探测器。

> 待本地验证：本实践不依赖 NPU 或 CANN 环境，仅需系统自带 Python 3。

#### 4.1.5 小练习与答案

**练习 1**：如果日志某行写作 `[Errorxyz]`（小写、非标准错误码），`get_error_type` 会返回什么？这条记录会被 `parse_log` 收集吗？

**答案**：`get_error_type` 只检查是否以 `[Error` 开头，不校验后面的字符串是否在 `err_details` 字典里，所以会返回 `Errorxyz`。该行会被 `parse_log` 当作错误行收集进 `err_info`。但到了报告生成阶段，`err_details.get("Errorxyz")` 返回 `None`，所以打印的 `Rule` 行会是 `Rule: None`，错误码本身仍会出现在统计里。

**练习 2**：为什么 `cce_intri` 用「暂存 + 在错误行命中时消费」的方式，而不是反过来？

**答案**：因为日志里内建函数签名行（`### ...`）总是写在它触发的错误行（`[Error...]`）**之前**。脚本逐行向前读，必须在读到签名时先存起来，等下一行读到错误时才能把两者配对。如果反过来（在错误行存、签名行消费），就配不上对了。

### 4.2 BackTrace 堆栈提取与去重 key

#### 4.2.1 概念说明

错误行告诉我们「**发生了什么**」（如 `ErrorRead3` 读取越界），内建函数签名告诉我们「**哪条指令出错**」（如 `vadd(...)`），但这还不够定位到「**我自己写的哪行代码**」。这时就需要 BackTrace 堆栈——它记录了从 npuchk 检测点一路回溯的调用链地址。

`parse_log` 的后半段专门处理堆栈段：识别堆栈段的开始与结束、逐帧解析出「二进制文件名 + 地址」、并用一个巧妙构造的 key 给相同堆栈去重。

#### 4.2.2 核心流程

堆栈段处理的状态转移如下：

```
进入条件: 某行 find("# BackTrace #") > 0   →  bs_start = True
逐帧处理: bs_start 期间、且行以两空格开头
          - 含 ".so" 的帧 → 跳过（库帧不定位源码）
          - 形如 "binfile(+addr)" → 拆出 binfile 与 addr
            err_info ← "binfile:addr"
            key      ← addr          （每个地址都拼进 key）
退出条件: bs_start 期间、且行不以两空格开头 → bs_start = False，提交记录
```

去重机制是本节的精髓。脚本用 **key = 错误码 + 所有堆栈地址** 作为这条错误记录的「指纹」：

\[ \text{key} = \text{err\_type} \;\Vert\; \text{addr}_1 \;\Vert\; \text{addr}_2 \;\Vert\; \cdots \;\Vert\; \text{addr}_n \]

其中 `\Vert` 表示字符串拼接。两条错误当且仅当「错误码相同 **且** 整条堆栈地址完全相同」时 key 相同。`parse_log` 只在 `stack.get(key) is None`（即该指纹首次出现）时才写入：

\[ \text{记录被保留} \iff \text{key 在 } stack \text{ 中此前不存在} \]

这等价于：**同一处源码反复触发的同一种错误，在报告里只出现一次**。例如一个 `for` 循环里每轮都越界读，日志可能记几十条，但报告里只会看到一条——因为调用栈地址完全一致，key 相同，后续都被丢弃。反之，两个不同堆栈位置的 `ErrorRead3` 会保留为两条。

#### 4.2.3 源码精读

堆栈段的开始与结束判断在 [parse_log 的第 51–60 行](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L51-L60)：

```python
if not bs_start and line.find("# BackTrace #") > 0:
    bs_start = True
    continue
if bs_start and not line.startswith("  "):    # 堆栈段结束
    bs_start = False
    if stack.get(key) is None:                 # 首次出现该指纹才提交
        stack[key] = err_info
    err_info = []
    key = ""
    continue
```

注意两个细节：

- `line.find("# BackTrace #") > 0` 要求标记不出现在行首（`> 0` 而非 `>= 0`）。这与日志里 BackTrace 标记行通常带有前导空格或前缀（如 ` # BackTrace #`）的格式相对应。
- 堆栈段结束的判定是「在段内、且当前行不以两个空格开头」。结束时会**提交并清空** `err_info` 与 `key`，为下一条错误做准备。这也是 4.1 里说「命中错误行时无需先清空 `err_info`」的原因——前一条此时已被清空。

逐帧解析在 [parse_log 的第 61–71 行](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L61-L71)：

```python
if bs_start:
    if line.find(".so") > 0:
        continue                               # 丢弃 .so 库帧
    line = line.strip()
    binfile = line.split("(")[0]               # "(" 之前是二进制文件名
    if line.find("+") < 0:
        continue                               # 无 "+" 的帧无法定位，跳过
    addr = line.split("+")[1].split(")")[0]    # "+" 之后、")" 之前是地址
    info_tmp = binfile + ":" + addr
    err_info.append(info_tmp)                  # 累积成 "binfile:addr"
    key += addr                                # 地址拼进去重 key
```

这里用两次字符串切分把 `./add(+0x4a3f)` 拆成 `binfile="./add"`、`addr="0x4a3f"`，再拼成 `./add:0x4a3f` 存进 `err_info`。注意它**同时**把 `addr` 追加进 `key`，这正是 4.2.2 里去重指纹的构造点——读帧和构造 key 是在同一次循环里同步完成的。

> **小结**：堆栈提取 = 「识别段起止 + 丢弃 .so + 拆 bin:addr + 拼去重 key」。`err_info` 在这段结束后变成了 `[错误行, 内建函数签名, bin1:addr1, bin2:addr2, ...]`，为 4.3 的报告拼装备好了全部原料。

#### 4.2.4 代码实践

**实践目标**：手工模拟 `parse_log` 对一段迷你日志的处理，验证去重 key 的行为。

**操作步骤**：

1. 把下面这段「示意日志」存成 `/tmp/fake_npuchk.log`（这是依据解析逻辑**构造的示意文件**，非真实产物）：

   ```
   ### vadd((__ubuf__ half*)0x1)
   [V] [ErrorRead3] on read 0x10 0x100
    # BackTrace #
     ./add(+0x111)
     ./add(+0x222)
    end
   ### vadd((__ubuf__ half*)0x2)
   [V] [ErrorRead3] on read 0x20 0x100
    # BackTrace #
     ./add(+0x111)
     ./add(+0x222)
    end
   ```

   > 两条记录的错误码与堆栈地址完全相同（模拟循环里同一处反复出错）。

2. 用 `parse_log` 解析并打印 key 与对应 err_info 长度：

   ```bash
   python3 -c "import importlib.util; \
   spec = importlib.util.spec_from_file_location('r', 'npuchk/ascendc_npuchk_report.py'); \
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); \
   s = {}; m.parse_log('/tmp/fake_npuchk.log', s); \
   print('记录数:', len(s)); \
   [print('key=', repr(k), '帧数=', len(v)) for k, v in s.items()]"
   ```

**需要观察的现象**：虽然日志里有两条错误记录，但 `parse_log` 解析后 `s` 里只有 **1 条**记录。

**预期结果**：打印 `记录数: 1`，且唯一的 key 形如 `ErrorRead30x1110x222`（错误码 + 两个地址拼接），帧数为 4（错误行 + 签名 + 两个堆栈帧）。这验证了「同错误码 + 同堆栈 → 去重为一条」。

> 待本地验证：本实践仅需 Python 3，不依赖 NPU 环境。

#### 4.2.5 小练习与答案

**练习 1**：若把上面示意日志里第二条记录的某个堆栈地址从 `0x222` 改成 `0x333`，`s` 里会有几条记录？为什么？

**答案**：会有 2 条。因为 key 由「错误码 + 全部地址」拼接，地址变化使 key 从 `ErrorRead30x1110x222` 变成 `ErrorRead30x1110x333`，两者不同，所以第二条不会被去重，各自保留。这也说明 key 的粒度精确到「整条堆栈」。

**练习 2**：为什么堆栈帧里含 `.so` 的行要被丢弃？

**答案**：含 `.so` 的帧通常属于闭源模型库（如 `libcpudebug_npuchk.so`）或系统库（如 libc）。这些帧既没有开源源码可供 `addr2line` 映射到读者关心的位置，也不是算子开发者的代码，保留它们只会污染报告。丢弃 `.so` 帧能让报告聚焦在用户自己的算子二进制（如 `./add`）上。

### 4.3 addr2line + c++filt 源码定位

#### 4.3.1 概念说明

经过 4.1 和 4.2，我们手里已经有了一条条 `err_info`，其中堆栈帧形如 `./add:0x4a3f`。最后一步是把 `0x4a3f` 这个地址翻译成 `KernelAdd::Compute at examples/02_cpudebug/add.asc:42` 这样的可读文本。这一步完全交给系统工具 `addr2line`（地址→函数名+文件:行号）和 `c++filt`（函数名反修饰）。

`ascendc_npuchk_report.py` 用 `subprocess` 调用这两个外部命令，并把结果拼装成最终报告。

#### 4.3.2 核心流程

地址到源码的翻译链如下：

```
binfile:addr   (来自 err_info[2:])
    │
    ├── execute_cmd(["addr2line", "-f", "-e", binfile, addr])
    │       输出两行:
    │         第1行 = 修饰(mangled)的函数名,  如 _ZN8KernelAdd7ProcessEv
    │         第2行 = 文件:行号,              如 /path/add.asc:42
    │
    ├── execute_cmd(["c++filt", func])   反修饰函数名
    │       _ZN8KernelAdd7ProcessEv  →  KernelAdd::Process()
    │
    └── 返回 "{func} at {file:line}"
```

`addr2line` 关键参数：

- `-f`：除了文件:行号，**也打印函数名**（否则只输出文件:行号，少了函数信息）。
- `-e <二进制>`：指定要查询的 ELF 文件。
- 最后的位置参数：要查询的地址（十六进制，如 `0x4a3f`）。

前提：被查询的二进制（如 CPU 域的 `./add`）必须是用 `-g` 编译、带 DWARF 调试信息的；否则 `addr2line` 只能给出 `? ?` 或 `??:0`。

#### 4.3.3 源码精读

先看外部命令执行器 [execute_cmd](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L74-L87)，它是对 `subprocess.Popen` 的薄封装：

```python
def execute_cmd(cmds):
    proc = subprocess.Popen(
        cmds, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8"
    )
    try:
        outs, errs = proc.communicate(timeout=10)   # 10 秒超时
        if len(errs) > 0:
            print(errs)
        return outs.strip()
    except subprocess.TimeoutExpired:
        proc.kill()
        outs, errs = proc.communicate()
        print("Error:\n", errs)
    return ""
```

两个要点：一是 `timeout=10` 防止某个 `addr2line` 调用卡死整个脚本（例如二进制路径不存在或文件损坏时）；二是超时后会 `proc.kill()` 并返回空串，调用方拿到空串后 `addr_to_line` 会返回 ` at `（空函数名空位置），不会让脚本崩溃。

再看 [addr_to_line](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L90-L100)，它把两步命令串起来：

```python
def addr_to_line(bin_file, addr):
    res = execute_cmd(["addr2line", "-f", "-e", bin_file, addr])
    fun_line = res.split("\n")                  # 第0行=函数名，第1行=文件:行号
    fun = ""
    line = ""
    if len(fun_line) > 0:
        fun = fun_line[0]
        fun = execute_cmd(["c++filt", fun])     # 反修饰函数名
    if len(fun_line) > 1:
        line = fun_line[1]
    return "{} at {}".format(fun, line)
```

`addr2line -f` 的输出固定是「函数名一行 + 文件:行号一行」，所以 `split("\n")` 后 `[0]` 取函数名、`[1]` 取位置。函数名再过一次 `c++filt` 去掉 C++ 修饰，最后用 `"{} at {}".format(...)` 拼成 `KernelAdd::Process at /path/add.asc:42` 这样的可读串。

最后看 `__main__` 块里如何把 `err_info` 拼成报告，关键在 [第 162–174 行](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L162-L174)：

```python
for frame in cur_err_info[2:]:                  # 从第3个元素起是堆栈帧 bin:addr
    info = frame.split(":")
    if len(info) < 2:
        stack_info.append("  " + info[0])
        continue
    if cpu_bin_path:                            # 场景3：用户显式给了二进制目录
        info[0] = os.path.join(cpu_bin_path, info[0])
    stack_info.append("  " + addr_to_line(info[0], info[1]))
stack_info.append("")
LOG = "\n".join(stack_info)
if LOG.find("PostMessage") > 0:                 # 含框架内部 PostMessage 的记录跳过
    continue
print(LOG)
```

这段把 `err_info` 的三段（错误行 / 内建函数签名 / 堆栈帧）按固定顺序拼进 `stack_info`：

- `cur_err_info[0]`：错误行（前面已 append）。
- `"Rule: " + err_details[err_type]`：错误码对应的中文规则解释。
- `cur_err_info[1]`：内建函数签名（如 `### vadd(...)`）。
- `cur_err_info[2:]`：逐帧调 `addr_to_line` 翻译。

`cpu_bin_path` 对应 [docs/02_npu_check.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md) 里脚本的第三种用法：当日志里的 `binfile` 是相对路径、但二进制实际在别处时，用 `os.path.join` 把用户给定的目录拼到前面。`LOG.find("PostMessage") > 0` 则把含框架内部 `PostMessage` 调用的记录整体跳过，避免噪声。

报告末尾的错误统计在 [第 175–178 行](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/npuchk/ascendc_npuchk_report.py#L175-L178)，按 `统计数, 错误码, 中文解释` 三段式打印，与 [docs/02_npu_check.md:136-144](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L136-L144) 给出的样例一致：

```
---------------------- ERROR STATISTICS ----------------------
1, ErrorBuffer2, VECIN/VECOUT/VECCALC的操作不合规
1, ErrorWrite1, 非法内存写入数据: ...
```

> **小结**：`addr_to_line` = `addr2line -f`（地址→修饰函数名+文件:行号）+ `c++filt`（反修饰）。`__main__` 把 `err_info` 的三段按「错误行 → Rule → 内建函数签名 → 逐帧源码定位」拼成报告，再附错误统计。

#### 4.3.4 代码实践

**实践目标**：亲手对一条 BackTrace 地址执行 `addr2line -f -e` + `c++filt`，验证结果与脚本 `addr_to_line` 的输出完全一致——这是本讲的核心实践。

**操作步骤**：

1. **准备一个带调试符号的 CPU 域二进制**。复用 u1-l4 / u2 的 add 样例，按 [docs/02_npu_check.md:116-121](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L116-L121) 的步骤编译（CPU 域编译默认带 `-g`）：

   ```bash
   cd examples/02_cpudebug
   mkdir -p build && cd build
   cmake .. -DSOC_VERSION=${SOC_VERSION}; make -j
   ./add
   ```

2. **生成一条真实的 npuchk.log**。参照 [docs/02_npu_check.md:94-110](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L94-L110)，在 `CopyIn` 里 `AllocTensor` 之后、`DataCopy` 之前插入一行 `inQueueX.FreeTensor(xLocal);` 构造错误，重新编译运行，得到 `npuchk/add_custom_0_0_vec_npuchk.log`。

3. **从日志里抄一条堆栈地址**。打开 log，找到 `# BackTrace #` 段下第一个非 `.so` 帧，记下它的 `binfile` 与 `addr`，例如 `./add(+0x4a3f)`。

4. **手动执行命令链**：

   ```bash
   # 第一步：addr2line（同时拿到函数名与文件:行号）
   addr2line -f -e ./add 0x4a3f
   # 输出形如：
   #   _ZN8KernelAdd7ProcessEv      ← 修饰的函数名
   #   /abs/path/add.asc:42         ← 文件:行号

   # 第二步：c++filt 反修饰函数名
   c++filt _ZN8KernelAdd7ProcessEv
   # 输出：KernelAdd::Process()
   ```

5. **与脚本对照**。用脚本解析同一份 log，对比手动结果：

   ```bash
   cd asc-tools
   python3 npuchk/ascendc_npuchk_report.py examples/02_cpudebug/build/npuchk/add_custom_0_0_vec_npuchk.log
   ```

**需要观察的现象**：报告里那一帧显示为 `KernelAdd::Process at /abs/path/add.asc:42`，与你手动跑 `addr2line` + `c++filt` 得到的函数名和行号**逐字一致**。

**预期结果**：手动命令链的输出（函数名 + 文件:行号）拼接后，等于脚本报告里对应那一行的内容。这证明 `addr_to_line` 只是把这两条命令串起来、没有做额外加工。

> 待本地验证：本实践需要先按 u1-l3 搭好 CANN 环境并编译出 CPU 域二进制；若仅做源码阅读，可跳到 4.3.5 的练习，对照函数逻辑理解即可。若 `addr2line` 返回 `? ?` 或 `??:0`，说明该二进制未带调试符号（缺 `-g`）或地址来自 `.so`，应换一条非 `.so` 帧。

#### 4.3.5 小练习与答案

**练习 1**：如果用户给的二进制没有用 `-g` 编译，`addr_to_line` 会返回什么？报告会因此崩溃吗？

**答案**：`addr2line` 对无调试信息的地址会返回 `??` 与 `??:0`，`c++filt` 对 `??` 基本原样返回，所以 `addr_to_line` 大致返回 `?? at ??:0`。脚本不会崩溃——`execute_cmd` 有 `timeout=10` 兜底，`addr_to_line` 的字符串拼接也对任意输入安全。报告里这一帧只是显示为 `?? at ??:0`，定位失败但不影响其他帧。

**练习 2**：为什么用 `addr2line -f` 而不是不带 `-f`？少 `-f` 会丢什么信息？

**答案**：不带 `-f` 时 `addr2line` 只输出 `文件:行号` 一行，没有函数名。`addr_to_line` 用 `fun_line[0]` 取函数名、`fun_line[1]` 取位置；若少 `-f`，`fun_line[0]` 会变成文件:行号、`fun_line[1]` 为空，拼出来的报告就丢了函数名且位置错位。`-f` 是让地址同时映射到「函数 + 位置」所必需的。

**练习 3**：`execute_cmd` 的 `timeout=10` 是针对什么风险设计的？

**答案**：防范外部命令（`addr2line` / `c++filt`）卡死。当传入的二进制路径不存在、文件损坏、或地址格式异常时，子进程可能挂起不返回；若无超时，整个解析脚本会无限期阻塞。`timeout=10` 保证单次调用最多等 10 秒，超时后 `proc.kill()` 回收子进程并返回空串，让脚本继续处理后续帧。

## 5. 综合实践

**任务：构造一个错误用例，端到端走完「生成 npuchk.log → 脚本解析 → 手动验证 addr2line」全链路。**

把本讲三个模块串起来：

1. **生成日志**（承接 u5-l1 与 [docs/02_npu_check.md:94-110](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L94-L110)）：在 `examples/02_cpudebug` 的 add 样例 `CopyIn` 里插入 `inQueueX.FreeTensor(xLocal);`，按 CPU 域编译运行，得到 `npuchk/add_custom_0_0_vec_npuchk.log`。

2. **脚本解析**（4.1 + 4.2）：

   ```bash
   python3 npuchk/ascendc_npuchk_report.py examples/02_cpudebug/build/npuchk/add_custom_0_0_vec_npuchk.log
   ```

   预期看到类似 [docs/02_npu_check.md:140-144](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/02_npu_check.md#L140-L144) 的统计：`ErrorBuffer2`（VECIN/VECOUT/VECCALC 操作不合规）与 `ErrorWrite1`（非法内存写入）。

3. **读懂报告结构**：对照 4.1.2 的状态机，确认报告每一条由「错误行 → Rule → 内建函数签名 `### ...` → 若干 `函数 at 文件:行号` 帧」组成。

4. **手动验证一帧**（4.3）：从原始 log 抄出对应帧的 `binfile(+addr)`，手动跑 `addr2line -f -e binfile addr` 与 `c++filt`，确认与报告那一行逐字相同。

5. **观察去重**：若你的错误在循环里反复触发，原始 log 里会有多条相同记录；确认脚本报告里只出现一条（4.2 的去重 key 生效）。

> 完成标志：你能指着报告里某一行 `XXX at add.asc:NN`，说清这一行的函数名来自 `c++filt`、行号来自 `addr2line`、而 `XXX` 这个地址是从原始 log 的 BackTrace 段按 `bin(+addr)` 格式拆出来的。待本地验证。

## 6. 本讲小结

- `ascendc_npuchk_report.py` 是把机器向的 `*_npuchk.log` 翻译成人读报告的唯一入口，核心是 `parse_log` 的**手写文本状态机**，靠 `### `、`[Error`、`# BackTrace #` 三个标记切分文本。
- `get_error_type` 同时承担「判断错误行」与「提取错误码」两个职责，是状态机的探测器；它只看 `[Error` 前缀，不校验错误码是否合法。
- `parse_log` 把每条错误打包成 `err_info = [错误行, 内建函数签名, bin1:addr1, bin2:addr2, ...]`，其中内建函数签名靠「暂存 + 错误行命中时消费」配对。
- 堆栈提取会丢弃 `.so` 库帧；去重 key = `错误码 + 全部堆栈地址`，使「同源反复触发的同一错误」在报告里只出现一次。
- `addr_to_line` 把 `addr2line -f`（地址→修饰函数名+文件:行号）和 `c++filt`（反修饰）串成地址翻译链；`execute_cmd` 用 `timeout=10` 防止外部命令卡死。
- 最终报告按「错误行 → Rule（中文解释）→ 内建函数签名 → 逐帧源码定位」拼装，末尾附错误统计；这一切的前提是 CPU 域二进制带 `-g` 调试符号。

## 7. 下一步学习建议

- **横向对照另一条「地址→源码」链路**：u3-l3 讲过的 `stub_backtrace.cpp` 用 `dladdr` + `addr2line` 在 C++ 侧做逆向回溯，与本讲 Python 侧的 `addr_to_line` 思路一致但实现语言不同，对照阅读能加深对 addr2line 用法的理解。
- **回到错误源头**：本讲只讲「读日志」，建议回到 u5-l1 重看一遍错误码语义（尤其 `ErrorRead2`/`ErrorWrite3` 这类「可疑问题」非致命提示），理解每种错误在算子源码里的典型成因。
- **向工具链下游**：npu check 之外，asc-tools 还有 `msobjdump`（u6）和 `show_kernel_debug_data`（u7）两个离线解析工具。它们的解析对象分别是 ELF 文件和 dump bin 文件，与本讲的「文本日志解析」属于同类工作（都是把机器产物翻译成人读结构），阅读时可相互参照解析手法。
