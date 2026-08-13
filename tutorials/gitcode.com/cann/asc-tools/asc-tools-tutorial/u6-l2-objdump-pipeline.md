# ObjDump 解析流程实现

## 1. 本讲目标

上一讲（u6-l1）我们弄懂了 Kernel ELF 的**静态结构**：标准 ELF64 + `.ascend.meta` / `.aicore_binary` 两个 Ascend 专属段，以及 meta 段里的 TLV（Type-Length-Value）元信息编码。本讲把视角从"文件长什么样"切换到"工具怎么读它"——

具体说，学完本讲你应该能够：

- 说清 `msobjdump` 命令从一条 shell 命令到打印结果的**完整调用链**，以及 `ObjDump` 类的职责。
- 区分 `--dump-elf` / `--extract-elf` / `--list-elf` 三种命令模式在**实现上的差异**（解析 vs 解压 vs 列表），以及它们如何针对四种交付件类型分流。
- 理解 TLV 解析与段提取的**二进制读取细节**，包括融合编译产物的 `.aicore_binary` 自动"套娃"提取。
- 准确回答一个易错问题：**`--verbose` 到底改变了什么打印范围**（提示：它不改变 meta 字段的打印）。

## 2. 前置知识

本讲是纯 Python 源码阅读，不依赖 NPU 环境，但需要以下基础：

- **ELF 与 readelf**：ELF（Executable and Linkable Format）是 Linux 上的可执行/可链接文件格式。`readelf` 是查看 ELF 结构的标准工具。本工具大量调用 `readelf` 的三个变体：
  - `readelf -SW`：只打印**段表**（Section Headers），`-W` 表示宽格式不截断。
  - `readelf -sW`：只打印**符号表**（Symbols）。
  - `readelf -aW`：打印**全部**信息（等价于 `-h -l -S -s -r ...`），输出最长。
- **llvm-objcopy**：LLVM 提供的目标文件复制工具，可把 ELF 的某个段单独抽出来转成原始二进制（`-O binary --only-section=...`）。
- **ar**：静态库（`.a`）解包工具，`ar x libfoo.a bar.o` 把 `bar.o` 从归档里取出来。
- **struct.unpack**：Python 标准库，按格式串（如 `"I"`=4 字节无符号整数、`"H"`=2 字节、`"<Q"`=小端 8 字节）从 `bytes` 里解包 C 语言布局的二进制数据。
- **mmap**：内存映射文件，把整个文件映射进内存后用切片下标读取，比 `seek/read` 更直观。
- **argparse 的 Action**：自定义 `argparse.Action` 子类，可以在命令行参数被解析时插入自定义校验逻辑（本工具用它做文件存在性检查）。

建议先回顾 u6-l1 的 TLV 概念与本讲的"融合编译套娃产物"，因为本讲直接复用这些结论。

## 3. 本讲源码地图

本讲只涉及 msobjdump 这个 Python 工具，源码集中在 `utils/msobjdump/msobjdump/` 下，一共三个 `.py` 文件加一个 shell 包装：

| 文件 | 作用 | 本讲角色 |
| ---- | ---- | ---- |
| `utils/msobjdump/msobjdump.sh` | 一行 shell 包装：`python3 -m msobjdump $@`，把命令名 `msobjdump` 映射到 Python 模块入口 | 命令入口 |
| `utils/msobjdump/msobjdump/__main__.py` | 模块入口（`python -m msobjdump` 执行的就是它），调用 `parse_args()` 后分发给 `entry_function` | 程序入口 |
| `utils/msobjdump/msobjdump/msobjdump_main.py` | **核心实现**，约 900 行：`ObjDump` 类、命令行解析、TLV 解析、段提取全在这里 | 本讲主角 |
| `utils/msobjdump/msobjdump/utils.py` | 对 `readelf` / `llvm-objcopy` / `ar` 等外部命令的薄封装 + 字符串小工具 | 外部命令层 |

阅读建议：先看 `__main__.py`（极短）建立入口印象，再跳到 `msobjdump_main.py` 的 `parse_args`（命令行）→ `run_obj_dump`（总入口）→ `ObjDump.__init__` / `run`（主流程）→ 三个 `_dump_elf_process` / `_extra_elf` / `_list_elf`（三种模式）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：入口与命令行解析 → ObjDump 主流程与类型探测 → 三种命令模式 → TLV 解析与段提取。

### 4.1 入口与命令行解析

#### 4.1.1 概念说明

`msobjdump` 在终端是一个命令，但它在源码里其实是一个 Python 包。CMake 构建时（见 `utils/msobjdump/CMakeLists.txt`）会把它打包成 wheel 并安装一个 shell 包装脚本 `msobjdump.sh`，于是用户敲 `msobjdump --dump-elf ./demo` 时，实际执行的是：

```bash
python3 -m msobjdump --dump-elf ./demo
```

`python -m msobjdump` 会自动运行包里的 `__main__.py`。这样就完成了"命令名 → Python 模块"的桥接。

#### 4.1.2 核心流程

入口链非常短：

1. `msobjdump.sh` 转发参数给 `python3 -m msobjdump`。
2. [`__main__.py` 的 `main()`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/__main__.py#L17-L19) 调用 `parse_args()` 得到 `args`，再调用 `args.entry_function(args)`。
3. `entry_function` 是谁？在 [`parse_args` 里的 `set_defaults`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L877) 被绑定为 `run_obj_dump`。
4. [`run_obj_dump`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L765-L771) 只做两件事：`ObjDump(args)` 构造、`objdump.run()` 执行。

用伪代码表示：

```
msobjdump.sh → python -m msobjdump
   → __main__.main()
       → args = parse_args()           # 解析 -d/-e/-l/-V/-o
       → args.entry_function(args)     # 即 run_obj_dump(args)
           → objdump = ObjDump(args)   # 构造时就完成类型探测
           → objdump.run()             # 分发到三种模式
```

#### 4.1.3 源码精读

命令行定义全部在 [`parse_args`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L828-L884)。它定义了四个互斥的"操作"参数和一个输出目录：

| 参数 | dest 名 | 含义 |
| ---- | ---- | ---- |
| `--dump-elf` / `-d` | `dump_elf` | 解析 ELF，打印元信息 |
| `--verbose` / `-V` | `verbose` | 配合 `-d`，额外打印全量 ELF 结构 |
| `--extract-elf` / `-e` | `extr_elf` | 把 ELF 内嵌的子文件解压落盘 |
| `--list-elf` / `-l` | `list_elf` | 只列出内含的子文件名 |
| `--out-dir` / `-o` | `out_dir` | 解压落盘目录 |

注意三个"文件类"参数都挂了同一个自定义 Action：`action=FileAction`。它的作用是在解析阶段就做**文件有效性校验**，见 [`FileAction.__call__`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L810-L825)：

```python
def __call__(self, parser, namespace, values, option_string=None):
    try:
        if values.endswith(KEY_A_FILE):     # ".a" 静态库归档
            get_o_file(values)              # 先用 ar x 解出 .o
            values = get_o_file(values)
        if not values or not os.path.exists(values):
            raise RuntimeError(...)
        else:
            setattr(namespace, self.dest, os.path.realpath(values))  # 存绝对路径
    except RuntimeError:
        print("[ERROR]: File does not exist or permission denied!!!")
```

两个要点：第一，输入文件路径会被规范化成绝对路径（`os.path.realpath`）后写回 namespace，所以后续代码拿到的 `self.obj` 一定是绝对路径；第二，如果传入的是 `.a` 静态库，会先用 `ar x` 把里面的 `.o` 抽出来再继续（单测 `test_file_action_only_parses_a_suffix_as_archive` 专门验证只有 `.a` 后缀才触发解包，`.axx` 不会）。

> 小细节：当三个操作参数都没给时，[`parse_args` 末尾的 `if len(sys.argv) == 1`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L878-L880) 会打印帮助并退出——这就是 `msobjdump -h` / 无参时看到帮助信息的原因。

#### 4.1.4 代码实践

**实践目标**：亲手验证"命令名 → Python 模块"的桥接，确认入口链。

**操作步骤**（无需 CANN 环境，纯 Python 即可）：

1. 在仓库根目录把工具目录加入 `PYTHONPATH`：
   ```bash
   export PYTHONPATH=utils/msobjdump:$PYTHONPATH
   ```
2. 直接以模块方式运行，不带任何参数：
   ```bash
   python3 -m msobjdump
   ```
3. 再用 `-h` 看帮助：
   ```bash
   python3 -m msobjdump -h
   ```

**需要观察的现象**：第 2、3 步都会打印 `argparse` 生成的帮助文本，开头是 `usage: msobjdump ...`，并列出 `--dump-elf/--verbose/--extract-elf/--list-elf/--out-dir` 五个参数。

**预期结果**：这证明 `python3 -m msobjdump` 确实进入了 `__main__.py` → `parse_args()`，且 `parse_args` 在无参时走 `parser.print_help()` 分支。这与 `msobjdump.sh` 的 `python3 -m msobjdump $@` 完全一致，只是省去了 shell 包装。

> 若环境里 `msobjdump` 命令未安装（没有 CANN toolkit），上述 `python3 -m msobjdump` 方式等价于运行命令行工具，是阅读本讲最轻量的验证手段。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__main__.py` 里写 `args.entry_function(args)`，而不是直接调用 `run_obj_dump(args)`？

**参考答案**：这是一种"依赖注入"写法。`entry_function` 通过 `parser.set_defaults(entry_function=run_obj_dump)` 绑定（见 [msobjdump_main.py:877](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L877)）。这样 `__main__.py` 不需要 `import` 具体函数，只依赖 `parse_args` 的返回值，入口模块与实现解耦，方便日后替换入口函数或做单测替换。

---

### 4.2 ObjDump 主流程与文件类型探测

#### 4.2.1 概念说明

msobjdump 要应对**至少四种**不同的算子交付件，它们的内部结构差别很大。`ObjDump` 类的设计哲学是："**先把输入文件归类，再用同一套三种命令模式分别处理每一类**"。也就是说，命令模式（dump/extract/list）是一个维度，文件类型是另一个维度，真正干活的方法是"模式 × 类型"的二维分流。

四种文件类型由 [`ObjType` 枚举](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L99-L103)定义：

| ObjType | 对应交付件 | 识别特征 |
| ---- | ---- | ---- |
| `TYPE_BINARY_O_JSON` | aclnn 打包交付件 | 符号表里有 `_o_start` / `_json_start`，内嵌若干 `.o` / `.json` 在 `.data` 段 |
| `TYPE_ASCEND_KERNEL` | 单算子编译交付件（fatbin） | 含 `.ascend.kernel.<name>` 段，段里打包了多个 kernel |
| `TYPE_ASCEND_META` | 单算子 ELF（仅元信息） | 含 `.ascend.meta.<name>` 段，无 kernel/binary |
| `TYPE_AICORE_BINARY` | 融合编译产物（套娃） | 含 `.aicore_binary` 段，该段本身就是内层 ELF |

#### 4.2.2 核心流程

`ObjDump` 的生命周期分三阶段，由 [`run`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L184-L187) 串联：

```
构造 __init__：
  ├─ _set_out_dir()            # 准备输出目录 + 带时间戳/pid 的临时目录
  └─ _set_parse_obj_and_mode() # 决定命令模式 + 探测文件类型 + (必要时)自动提取

执行 run()：
  ├─ _parse_process()          # 按"模式"分发到 _dump_elf_process / _extra_elf / _list_elf
  └─ _clean()                  # 删除临时目录
```

其中"决定模式 + 探测类型"是关键，发生在构造阶段。[`_set_parse_obj_and_mode`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L242-L273) 的核心逻辑：

```python
if args.dump_elf:
    if args.verbose:
        self.parse_obj_mode = ParseObjMode.MODE_VERBOSE   # -d -V
    else:
        self.parse_obj_mode = ParseObjMode.MODE_DUMP_ELF  # -d
elif args.extr_elf:
    self.parse_obj_mode = ParseObjMode.MODE_EXTRA_ELF     # -e
elif args.list_elf:
    self.parse_obj_mode = ParseObjMode.MODE_LIST_ELF      # -l

self.src_obj = self.obj
self._detect_obj_type()                                   # 探测类型
if self.obj_type == ObjType.TYPE_AICORE_BINARY:
    self.obj = self._extract_aicore_binary()              # 套娃自动拆层
```

两个关键点：

1. **`--verbose` 不是独立的模式，而是 `--dump-elf` 的修饰**。它把 `MODE_DUMP_ELF` 升级为 `MODE_VERBOSE`，两者后续都走 `_dump_elf_process`，只是内部多打印一些东西（详见 4.3）。
2. **融合编译产物会被自动"剥壳"**。如果探测到 `.aicore_binary`，构造阶段就调用 `_extract_aicore_binary()` 把内层 ELF 抽出来，把 `self.obj` 替换成抽出来的文件；但 `obj_type` 仍保持 `TYPE_AICORE_BINARY`，以便后续用专属分支处理（如打印 ELF header、对 op 级 meta 不做条目数截断）。

类型探测由 [`_detect_obj_type`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L275-L288) 完成，本质上就是两次 `readelf` + 字符串匹配：

```python
output = utils.get_symbols_in_file(self.obj)          # readelf -sW
if "_o_start" in output or "_json_start" in output:
    self.obj_type = ObjType.TYPE_BINARY_O_JSON
output = utils.get_section_headers_in_file(self.obj)  # readelf -SW
if KEY_ASCEND_META in output:     self.obj_type = ObjType.TYPE_ASCEND_META
if KEY_ASCEND_KERNEL in output:   self.obj_type = ObjType.TYPE_ASCEND_KERNEL
if KEY_AICORE_BINARY in output:   self.obj_type = ObjType.TYPE_AICORE_BINARY
```

注意这是**顺序覆盖**：`KEY_ASCEND_BINARY`（`.aicore_binary`）最后判断，优先级最高。这是有意为之——融合编译产物里往往也含 `.ascend.meta`，但必须按"套娃"处理，所以 `.aicore_binary` 一票否决前面的判断。

#### 4.2.3 源码精读

"剥壳"逻辑 [`_extract_aicore_binary`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L290-L311) 调用 [`utils.extract_aicore_binary_from_elf`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py#L69-L81)，本质是：

```python
subprocess.run(["llvm-objcopy", "-O", "binary",
                "--only-section=.aicore_binary", input_file, output_file], ...)
```

`llvm-objcopy -O binary --only-section=.aicore_binary` 的语义是"只把 `.aicore_binary` 这一段的原始字节拷出来，丢掉所有 ELF 头信息"。由于这一段本身就是个完整的内层 ELF（u6-l1 讲过的"套娃"），抽出来后就得到一个真正可解析的 Kernel ELF。函数对三种失败都做了兜底：`llvm-objcopy` 不存在（`FileNotFoundError`）、返回非 0（`returncode != 0`）、产物为空（`getsize == 0`），分别抛出明确的 `RuntimeError`，单测 `test_extract_aicore_binary_edge_cases` 覆盖了这三类。

输出目录与临时目录由 [`_set_out_dir`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L225-L240) 建立。临时目录名是 `objdump_<时间戳>_<pid>`：

```python
self.tmp_dir = os.path.join(
    self.out_dir, "objdump_" + time.strftime("%Y%m%d_%H%M%S") + "_" + str(os.getpid()))
```

带 `pid` 是为了**支持多用户/多进程同时调用**（`run_obj_dump` 的文档字符串明确写了"支持多用户同时调用"），避免临时目录撞名。运行结束 [`_clean`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L324-L326) 会 `shutil.rmtree(self.tmp_dir)` 把它删掉，所以中间产物（解压出的 `.o`）默认不保留，除非用 `-e` 显式拷到 `out_dir`。

分发器 [`_parse_process`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L313-L322) 只是一层 if：

```python
if mode in (MODE_DUMP_ELF, MODE_VERBOSE): self._dump_elf_process()
elif mode == MODE_EXTRA_ELF:               self._extra_elf()
elif mode == MODE_LIST_ELF:                self._list_elf()
```

#### 4.2.4 代码实践

**实践目标**：通过阅读单测，理解 `_detect_obj_type` 如何被 readelf 的输出驱动，而不必真的有一个算子 ELF。

**操作步骤**：

1. 打开 [test_msobjdump.py 的 `test_dump_elf_ascend_kernel`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/msobjdump/test_msobjdump.py#L227-L255)，关注它给 `mock_section`（即 `readelf -SW` 的返回）填的字符串：
   ```
   [23] .ascend.kernel.ascend910b1.ascendc_kernels_npu PROGBITS ...
   ```
2. 问自己：这条字符串里含有 `KEY_ASCEND_KERNEL`（`.ascend.kernel.`），不含 `.aicore_binary`，所以 `_detect_obj_type` 会把 `obj_type` 设成什么？
3. 再看 [`test_dump_elf_fusion_compile`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/py_ut/testcase/msobjdump/test_msobjdump.py#L413-L453) 里 `mock_section.side_effect` 的第一段：
   ```
   [26] .aicore_binary    PROGBITS ...
   ```
   这条同时不含 `.ascend.kernel.` / `.ascend.meta.`，含 `.aicore_binary`，类型又会被设成什么？后续会不会触发 `mock_run`（即 `llvm-objcopy`）？

**需要观察的现象**：第一个用例走 `TYPE_ASCEND_KERNEL` 分支，不会调用 `extract_aicore_binary_from_elf`；第二个用例走 `TYPE_AICORE_BINARY` 分支，断言 `self.assertTrue(mock_run.called)` 说明必然调用了 `llvm-objcopy` 剥壳。

**预期结果**：你能用一句话复述"`.aicore_binary` 优先级最高，命中即自动套用 llvm-objcopy 提取"这条规则，并能解释为什么融合编译用例要多 mock 一个 `extract_aicore_binary_from_elf`。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 ELF 同时含有 `.ascend.meta.xxx` 和 `.aicore_binary` 两段，`_detect_obj_type` 最终判定的类型是什么？为什么这样设计？

**参考答案**：会判定为 `TYPE_AICORE_BINARY`。因为 [`_detect_obj_type`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L283-L288) 里 `.aicore_binary` 的判断在最后，覆盖前面的赋值。这样设计是因为融合编译产物是"套娃"：外层 `.aicore_binary` 才是真正的入口，必须先剥壳才能拿到内层 ELF；如果错判成 `TYPE_ASCEND_META`，就会去解析外层那些不完整的 meta，结果会错乱。

**练习 2**：临时目录名为什么要拼 `os.getpid()`？

**参考答案**：为了让多用户/多进程并发调用 `msobjdump` 时临时目录互不覆盖。`time.strftime` 精确到秒，同秒内两个进程仍会撞名，加上 `pid`（进程号全局唯一）才能彻底避免冲突。结束后 `_clean` 再统一删除。

---

### 4.3 三种命令模式实现

#### 4.3.1 概念说明

三种命令模式对应三种用户意图：

- **`--dump-elf`（解析）**：把 ELF 里的元信息**打印到终端**给人看，不产生文件（除非内嵌子文件需要临时落盘再读）。
- **`--extract-elf`（解压）**：把 ELF 里**内嵌的子文件**（`.o` / `.json` / 内层 kernel）抽出来**拷贝到 `out_dir`**，产生交付物。
- **`--list-elf`（列表）**：最轻量，只**打印子文件的名字清单**，不解析内容、不落盘。

三者的**计算开销**依次递减：dump 要解析 TLV、extract 要落盘、list 只需扫段表。三者都按 `obj_type` 二次分流。

#### 4.3.2 核心流程

以最复杂的 [`_dump_elf_process`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L328-L352) 为例，它是一个"模式 × 类型"的二维 switch：

```
_dump_elf_process():
  if TYPE_BINARY_O_JSON:
      解析内嵌符号 → 落盘临时 .o/.json → 对每个 .o 打印 meta
      if MODE_VERBOSE: 额外打印每个 .o 的 readelf -aW
  elif TYPE_ASCEND_KERNEL:
      _parse_ascend_kernel_infos()                # 扫 .ascend.kernel 段表
      _parse_elf_ascend_kernel_by_type(., "dump") # 切开每个 kernel 落临时 .o
      _show_elf_ascend_kernel_obj()               # 打印 VERSION/TYPE/LEN/meta
  elif TYPE_ASCEND_META:
      _show_elf_ascend_meta_obj()                 # 直接打印 meta TLV
  elif TYPE_AICORE_BINARY:
      _show_elf_ascend_meta_obj()                 # 对剥壳后的内层 ELF 打印 meta
      if MODE_VERBOSE: 额外打印 readelf -aW
```

`_extra_elf` 与 `_list_elf` 的分流结构几乎一模一样（见 [msobjdump_main.py:354-380](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L354-L380)），区别只在每个分支调用的"动作方法"不同：

| 类型 \ 模式 | dump（解析） | extract（解压） | list（列表） |
| ---- | ---- | ---- | ---- |
| BINARY_O_JSON | `_save_dump_elf_o_json` + 打印 meta | `_move_file_to_outdir_o_json` | `_list_elf_info_aclnn_pkg_obj` |
| ASCEND_KERNEL | 落临时 .o + `_show_elf_ascend_kernel_obj` | `_move_file_to_outdir_ascend_kernel` | `_parse_elf_ascend_kernel_by_type(., "list")` |
| ASCEND_META | `_show_elf_ascend_meta_obj` | `[WARNING]: nothing to extra` | `[WARNING]: nothing to list` |
| AICORE_BINARY | `_show_elf_ascend_meta_obj` (+verbose) | `_move_extracted_aicore_binary_to_outdir` | `_list_extracted_aicore_binary` |

从这张表能读出几个有用结论：

- **单算子 ELF（ASCEND_META）没有内嵌子文件**，所以 extract/list 都直接打 WARNING，只有 dump 有意义。
- **ASCEND_KERNEL 是"一对多"**：一个 `.ascend.kernel` 段里打包了若干 kernel，extract 会落盘多个 `.o`，list 会打印多行 `ELF file N: ...`。
- **AICORE_BINARY 剥壳后**，extract 把剥出来的 `.aicore.o` 整体落盘，list 只打印一行（剥壳产物视为单个文件）。

#### 4.3.3 源码精读

`--verbose` 对打印范围的影响**只出现在三处**，全部是"额外打印 `readelf -aW` 全量信息"，与 meta TLV 无关：

1. [`_dump_elf_process` 的 BINARY_O_JSON 分支](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L336-L337)：verbose 时调用 `_show_binary_o_json_obj`，对每个 `.o` 打印 `readelf -aW`。
2. [`_dump_elf_process` 的 AICORE_BINARY 分支](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L348-L350)：verbose 时打印 `====== [elf header infos] ======` + `get_all_section_symbols_in_file`。
3. [`_show_elf_ascend_kernel_obj`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L556-L560)：verbose 时对每个 kernel 文件打印 `readelf -aW`。

而所有 meta TLV 的打印方法（`_show_ascend_meta_tlv`、`_show_ascend_meta_op_tlv`、`_get_elf_ascend_meta_tlv`、`_get_elf_ascend_meta_op_tlv`）里**找不到任何 `MODE_VERBOSE` 判断**。这正是 docs/03 表格里"打印说明"列的含义：所有 meta 字段（VERSION、KERNEL_TYPE、MIX_TASK_RATION、DEBUG、DYNAMIC_PARAM 等）"不设置 `--verbose`，默认打印"；唯独 `elf header infos` 那一行写的是"设置 `--verbose`，开启全量打印"。

> 换句话说：**`--verbose` 不改变 meta 信息的打印范围，它只是额外追加了一段原始 ELF 结构（ELF Header / Section Headers / Symbol 表）的 `readelf -aW` 输出。** 这是本讲最容易被docs措辞误导的地方，务必记牢。

list 模式最轻量。对 ASCEND_KERNEL，[`_parse_ascend_kernel_content` 在 `parse_type == "list"` 时](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L726-L727)只 `print("ELF file ...")`，不写文件：

```python
if parse_type == "list":
    print("ELF file    " + str(kernel_id) + ": " + file_name)
else:  # dump
    file_name = os.path.join(self.tmp_dir, file_name)
    with open(file_name, "ab") as f:
        f.write(content[read_len : read_len + kernel_len_real])
```

extract 模式则在 dump 已落临时文件的基础上，多一步"搬到 out_dir"：[`_move_file_to_outdir_ascend_kernel`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L428-L436) 把每个 kernel 临时 `.o` 拷到输出目录。单测 `test_extr_elf_ascend_kernel` 断言输出目录下出现了 `ascend910b1_ascendc_kernels_npu_0_mix.o`，正好印证了 kernel 文件命名规则：`<obj_name>_<kernel_id>_<kernel_type>.o`。

#### 4.3.4 代码实践

**实践目标**：追踪 `_dump_elf_process` 与 `_parse_ascend_kernel_infos`，亲手验证"`--verbose` 只追加 ELF header 信息，不改 meta 打印"这一结论。

**操作步骤**（源码阅读型，无需运行环境；若有环境可做第 5 步验证）：

1. 在 [`_dump_elf_process`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L328-L352) 内搜索 `MODE_VERBOSE`，记录每个命中点的 obj_type 分支，以及它额外调用了什么。你会找到 BINARY_O_JSON 与 AICORE_BINARY 两处。
2. 进入 [`_show_elf_ascend_kernel_obj`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L539-L560)，再搜一次 `MODE_VERBOSE`，记录它额外调用的 `utils.get_all_section_symbols_in_file`（即 `readelf -aW`）。
3. 打开 [`_parse_ascend_kernel_infos`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L506-L537)，确认它的职责只是"扫 `readelf -SW` 输出，把每个 `.ascend.kernel.<name>` 段的 addr/offset/size 收集成字典"，**完全没有 verbose 判断**。
4. 再打开四个 meta 打印方法（`_show_ascend_meta_tlv` 等），用编辑器搜索 `VERBOSE`，确认命中数为 0。
5. （可选运行验证，待本地验证）按 `examples/04_msobjdump/README.md` 编译出融合产物 `./demo`，分别执行：
   ```bash
   msobjdump --dump-elf ./demo           > without_verbose.txt
   msobjdump --dump-elf ./demo --verbose > with_verbose.txt
   diff without_verbose.txt with_verbose.txt
   ```

**需要观察的现象**：第 1～4 步的源码搜索会证明 meta 打印路径里不存在 verbose 判断。第 5 步（若能运行）的 diff 会显示：两边都有 `.ascend.meta META INFO`、`VERSION`、`KERNEL_TYPE`、`MIX_TASK_RATION` 等行；`with_verbose.txt` 只比另一方**多出** `====== [elf header infos] ======` 及其后的一大段 `ELF Header / Section Headers / Symbol 表`。

**预期结果**：你能用一句话准确回答——"`--verbose` 不影响 meta 信息打印范围，仅在 dump 模式下追加 `readelf -aW` 的全量 ELF 结构信息"。这与 docs/03 字段表里"elf header infos 需要 `--verbose`"那一行完全对应。

#### 4.3.5 小练习与答案

**练习 1**：对单算子 ELF（`TYPE_ASCEND_META`）执行 `msobjdump --extract-elf xxx.o` 会发生什么？为什么？

**参考答案**：会打印 `[WARNING]: nothing to extra in single op elf file`，不产生任何文件。见 [`_extra_elf` 的 ASCEND_META 分支](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L365-L366)。因为单算子 ELF 本身就是最终产物，没有内嵌子文件可解压；它的价值在 meta 信息（用 `--dump-elf` 看），不在解压。

**练习 2**：`--list-elf` 对 `TYPE_ASCEND_KERNEL` 会打印几行 `ELF file ...`？由什么决定？

**参考答案**：打印的行数等于 `.ascend.kernel` 段里打包的 kernel 个数，即 `type_cnt`。因为 [`_parse_ascend_kernel_content`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L709-L727) 按 `for kernel_id in range(type_cnt)` 循环，list 模式下每个 kernel 打印一行。

---

### 4.4 TLV 解析与段提取

#### 4.4.1 概念说明

msobjdump 的"解析"本质是两件事：**从 ELF 段里读出原始字节**，再**按 TLV 协议把这些字节解释成人话**。本模块讲清楚两套 TLV（Function Meta / Binary Meta）的打印，以及两套"内嵌子文件"（`.ascend.kernel` 打包 / `.data` 段内嵌 o_json）的切割。

回顾 u6-l1：TLV = Type（2 字节）+ Length（2 字节）+ Value（Length 字节）。解析器只需一个游标 `index`，循环 `读 4 字节头 → 读 Length 字节值 → 推进游标`，遇到未知 Type 靠 Length 跳过即可，天然前向兼容。

msobjdump 区分两套 TLV，对应两个不同的段：

- **Function Meta（`.ascend.meta.<kernel名>` 段）**：描述**单个 kernel** 的属性，如核类型、核占比。Type 常量前缀 `F_TYPE_*`，映射表 [`F_TYPE_MAP`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L55-L63)。
- **Binary Meta（`.ascend.meta` 段，无后缀）**：描述**整个算子二进制**的属性，如版本、调试开关、动态参数。Type 常量前缀 `B_TYPE_*`，映射表 [`B_TYPE_MAP`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L72-L78)。

#### 4.4.2 核心流程

**meta 段定位与读取**统一在 [`_show_elf_ascend_meta_obj`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L610-L630) 里：先 `readelf -SW` 扫所有段名，挑出含 `.ascend.meta` 的段，记录每个段的 `(addr, offset, size)`；再分别交给 op 级和 function 级两个 TLV 解析器。字节读取由 [`_get_segment_content`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L739-L746) 用 `mmap` 切片完成：

```python
def _get_segment_content(self, offset, size):
    file_size = int(os.path.getsize(self.obj))
    with open(self.obj, "r+b") as f:
        mm = mmap.mmap(f.fileno(), file_size)
        return mm[offset : (offset + size)]   # 文件偏移直接切片
```

注意这里传入的 `offset` 是**文件偏移**（file offset），不是虚拟地址。段表里的 `offset` 字段正好就是文件偏移，所以可以直接用。

**Function Meta TLV 解析**在 [`_get_elf_ascend_meta_tlv`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L562-L584)，核心循环：

```
index = 0
while index < len(content):
    t, tlv_len = struct.unpack("2H", content[index:index+4])   # 读 TLV 头
    index += 4
    if index + tlv_len <= len(content):
        _show_ascend_meta_tlv(content, t, tlv_len, index)      # 按 t 解释 value
    index += tlv_len                                            # 推进游标
```

[`_show_ascend_meta_tlv`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L147-L169) 是一个按 `t` 分发的 switch，每个 Type 用不同的 `struct.unpack` 格式解释 Value：

| F_TYPE | 名称 | 解包格式 | 输出示例 |
| ---- | ---- | ---- | ---- |
| `1` KTYPE | KERNEL_TYPE | `I` → `K_TYPE_MAP` | `KERNEL_TYPE: MIX_AIC_MAIN` |
| `2` CROSS_CORE_SYNC | 硬同步 | `I` → `C_TYPE_MAP` | `CROSS_CORE_SYNC: USE_SYNC` |
| `3` MIX_TASK_RATION | 核占比 | `2H` | `MIX_TASK_RATION: [1:2]` |
| `11` ENABLE_EARLY_START | 早启动 | `I` | `ENABLE_EARLY_START: 1` |
| `13` DETERMINISTIC_INFO | 确定性计算 | `I` | `DETERMINISTIC_INFO: 1` |
| `14` FUNCTION_ENTRY | TilingKey | `<Q`（跳 4 字节） | `FUNCTION_ENTRY: 0x...` |
| `15` BLOCK_NUM | 执行核数 | （不读，固定值） | `BLOCK_NUM: 0xFFFFFFFF` |

> `BLOCK_NUM` 比较特殊：代码注释和 docs 都说明"该字段当前暂不支持，只打印默认值 `0xFFFFFFFF`"，所以 [L167-L169](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L167-L169) 直接硬编码输出，不读 Value。

**Binary Meta TLV 解析**在 [`_get_elf_ascend_meta_op_tlv`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L586-L608)，循环结构相同，但多了一个条目数上限：

```python
while index < len(content) and (idx < 4 or self.obj_type == ObjType.TYPE_AICORE_BINARY):
    ...
    idx += 1
```

含义是：普通 ELF 只打印 op 级 meta 的**前 4 条**；但融合编译产物（`TYPE_AICORE_BINARY`，剥壳后的内层 ELF）**不截断**，全部打印。此外，op 级 TLV 还有一个**去重**机制：[`_print_ascend_meta_op_tlv`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L218-L223) 在 `TYPE_AICORE_BINARY` 下用一个 `_aicore_binary_meta_printed` 集合去重，避免融合产物里重复的 RUNTIME_IMPLICIT_INFO 刷屏（单测 `test_show_ascend_meta_op_tlv_dedup_for_aicore_binary` 验证：连续两次 VERSION:1 只输出一次）。docs/03 样例输出里 `.ascend.meta META INFO` 段那一串 `RUNTIME_IMPLICIT_INFO` 正是这条路径打印的。

**`.ascend.kernel` 段切割**在 [`_parse_ascend_kernel_content`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L691-L737)。该段的二进制布局是"头 + 数组"：

```
┌──────────┬───────────┬─────────────────────────────────────┐
│ version  │ type_cnt  │  kernel[0]  kernel[1] ...            │
│ uint32   │ uint32    │  每个 kernel 见下                    │
└──────────┴───────────┴─────────────────────────────────────┘

每个 kernel:
┌──────────────┬────────────┬──────────────────┬────────────────────┐
│ kernel_type  │ kernel_len │ kernel_len_real  │ kernel 字节流       │
│ uint32       │ uint32     │ uint32           │ (取前 kernel_len_real)│
└──────────────┴────────────┴──────────────────┴────────────────────┘
```

注意 `kernel_len`（对齐后的段长度，游标按它推进）与 `kernel_len_real`（真正有效的字节数）的区别：写文件只用 `kernel_len_real` 字节，但游标要跳过 `kernel_len` 字节才能对齐到下一个 kernel。`KERNEL_TYPE_MAP`（`0=mix/1=aiv/2=aic`）把 `kernel_type` 翻译成人类可读串，用于拼文件名 `<obj_name>_<id>_<type>.o`。所有整数读取都过 [`_unpack_buff_content_by_type`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L171-L182)，它做边界检查，越界抛 `RuntimeError: ... out of bound`（单测 `test_unpack_buff_content_by_type_out_of_bounds` 覆盖）。

**aclnn 内嵌 o_json 切割**是另一套机制。aclnn 交付件把若干 `.o` / `.json` 用 `_binary_<name>_o_start/_end/_size` 符号嵌在 `.data` 段里。[`_parse_binary_o_json_obj`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L454-L488) 从 `readelf -sW` 符号表里挖出每个内嵌文件的 start/size（虚拟地址），再由 `_save_dump_elf_o_json` 把虚拟地址换算成文件偏移切出字节。换算公式是本模块唯一的数学点：

\[
\text{fileOffset} = \text{symbolAddr} + \text{dataOffset} - \text{dataAddr}
\]

即"符号虚拟地址"减去"`.data` 段的起始虚拟地址 `dataAddr`"，再加上"`.data` 段在文件里的偏移 `dataOffset`"，就得到该符号在**文件中的字节位置**。`dataAddr / dataOffset / dataSize` 由 [`get_data_segment_range`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L749-L762) 从 `readelf -SW` 的 `.data` 行解析。

#### 4.4.3 源码精读

把 [`utils.py`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py#L47-L81) 和 `msobjdump_main.py` 对照看，会发现整个工具其实是"Python 调度 + 外部命令干活"的薄封装：

| `utils.py` 函数 | 包装的命令 | 调用方 |
| ---- | ---- | ---- |
| [`get_section_headers_in_file`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py#L47-L50) | `readelf -SW` | 类型探测、段定位、kernel 信息收集 |
| [`get_symbols_in_file`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py#L53-L56) | `readelf -sW` | o_json 符号挖掘 |
| [`get_all_section_symbols_in_file`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py#L59-L62) | `readelf -aW` | `--verbose` 的全量打印 |
| [`extract_aicore_binary_from_elf`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py#L69-L81) | `llvm-objcopy -O binary --only-section=.aicore_binary` | 融合产物剥壳 |

而 [`split_str_with_space`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py#L19-L21) 与 [`get_str_between`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/utils.py#L24-L29) 则是解析 `readelf` 文本输出的小工具——msobjdump 把 `readelf` 的文本当"半结构化数据"用正则/分词来切。这也解释了为什么它对 `readelf` 输出格式有隐含依赖：一旦 `readelf` 版本变了输出列序调整，`ascend_kernel_name_id + addr_offsize` 这种"按列下标取值"的代码（见 [`_parse_ascend_kernel_infos`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L518-L531)）就需要相应调整。

#### 4.4.4 代码实践

**实践目标**：用 Python 亲手解一段 TLV，验证 4.4.2 描述的 Function Meta 解析逻辑，理解 `struct.unpack` 与游标推进。

**操作步骤**（纯 Python，可在任意装了 Python3 的机器运行）：

1. 把下面这段"示例代码"存成 `tlv_demo.py`（**注意：这是为讲解手写的示例代码，不是项目源码**）：
   ```python
   import struct

   # 构造一段假的 Function Meta TLV：KTYPE=4(MIX_AIC_MAIN) + MIX_TASK_RATION=[1:2]
   # F_TYPE_KTYPE=1, length=4, value=4  → KERNEL_TYPE: MIX_AIC_MAIN
   # F_TYPE_MIX_TASK_RATION=3, length=4, value=(1,2) → MIX_TASK_RATION: [1:2]
   K_TYPE_MAP = {"1": "AICORE", "2": "AIC", "3": "AIV",
                 "4": "MIX_AIC_MAIN", "5": "MIX_AIV_MAIN"}
   F_TYPE_MAP = {1: "KERNEL_TYPE", 3: "MIX_TASK_RATION"}

   content = b""
   content += struct.pack("2H", 1, 4) + struct.pack("I", 4)      # KTYPE
   content += struct.pack("2H", 3, 4) + struct.pack("2H", 1, 2)  # RATION

   index = 0
   while index < len(content):
       t, tlv_len = struct.unpack("2H", content[index:index+4])
       index += 4
       if t == 1:
           (v,) = struct.unpack("I", content[index:index+4])
           print(f"{F_TYPE_MAP[t]}: {K_TYPE_MAP[str(v)]}")
       elif t == 3:
           v1, v2 = struct.unpack("2H", content[index:index+4])
           print(f"{F_TYPE_MAP[t]}: [{v1}:{v2}]")
       index += tlv_len
   ```
2. 运行：`python3 tlv_demo.py`
3. 把构造的 `content` 与项目里 [`_show_ascend_meta_tlv`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L147-L169) 的解包格式逐行对照。

**需要观察的现象**：程序输出两行：
```
KERNEL_TYPE: MIX_AIC_MAIN
MIX_TASK_RATION: [1:2]
```

**预期结果**：输出与 docs/03 里融合编译样例的 meta 行格式完全一致。这说明你已掌握 TLV 游标循环：每轮先读 4 字节头 `(type, length)`，再按 `type` 选格式读 `length` 字节值，最后游标推进 `4 + length`。把这个循环套到真实 `.ascend.meta` 段字节上，就是 msobjdump 的 `_get_elf_ascend_meta_tlv`。

> 待本地验证：若想用真实数据，可对一个已知 kernel ELF 用 `readelf -SW` 找到 `.ascend.meta.xxx` 段的 offset/size，用 `dd` 或 Python `mmap` 抽出该段字节，再喂给上面的循环。

#### 4.4.5 小练习与答案

**练习 1**：`_get_elf_ascend_meta_op_tlv` 里为什么对普通 ELF 限制 `idx < 4`，而对 `TYPE_AICORE_BINARY` 不限制？

**参考答案**：普通单算子 ELF 的 op 级 meta 字段（VERSION/DEBUG/DYNAMIC_PARAM/OPTIONAL_PARAM）数量有限且固定，前 4 条已足够，多余的是历史/未知字段，截断可避免噪音。而融合编译产物剥壳后的内层 ELF 携带较多 `RUNTIME_IMPLICIT_INFO`（如 L2Cache Hint、Hardware Sync 等），这些对理解融合 kernel 很重要，故不截断、全量打印（配合 `_aicore_binary_meta_printed` 去重）。见 [msobjdump_main.py:597-608](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L597-L608)。

**练习 2**：切割 `.ascend.kernel` 段时，为什么写文件用 `kernel_len_real`，而推进游标用 `kernel_len`？

**参考答案**：`kernel_len` 是对齐后的段长度（可能含 padding），`kernel_len_real` 是真正有效的字节数。写文件只关心有效字节，所以用 `kernel_len_real`（见 [L731](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L720-L732) `content[read_len:read_len+kernel_len_real]`）；但下一个 kernel 紧跟在 padding 之后，游标必须按 `kernel_len` 推进才能对齐到下一个 kernel 的头部，否则后续解包全错位。

**练习 3**：`_get_segment_content` 用 `mmap` 切片读字节，传的是文件偏移还是虚拟地址？为什么不会错？

**参考答案**：传的是**文件偏移**（file offset）。因为调用方（如 `_get_elf_ascend_meta_tlv`）传入的 `meta_lists[1]/[2]` 来自 `readelf -SW` 段表的 offset/size 列，那一列本来就是文件偏移，与 `mmap` 的字节下标语义一致，所以可以直接切片。唯一要做虚拟地址→文件偏移换算的是 aclnn 的 o_json 路径（公式见 4.4.2）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次"**只看源码、画出完整数据流图**"的训练。

**任务**：针对融合编译产物（`TYPE_AICORE_BINARY`，即 `examples/04_msobjdump` 的 `./demo`），画出 `msobjdump --dump-elf ./demo --verbose` 从命令行到终端输出的**完整调用图**，标注每一步调用了哪个外部命令、读了哪个段、打印了什么。

**要求**：

1. 在图上标出下列节点（用箭头串起）：
   - `msobjdump.sh` → `__main__.main` → `parse_args` → `run_obj_dump` → `ObjDump.__init__` → `_set_parse_obj_and_mode` → `_detect_obj_type` → `_extract_aicore_binary` → `run` → `_parse_process` → `_dump_elf_process` → `_show_elf_ascend_meta_obj` → `_get_elf_ascend_meta_op_tlv` / `_get_elf_ascend_meta_tlv`。
2. 在每个调用了外部命令的节点旁，标注命令（`readelf -SW` / `readelf -sW` / `readelf -aW` / `llvm-objcopy`）。
3. 用不同颜色/记号区分两类输出：**meta TLV 行**（不带 verbose 也有）与 **elf header infos 行**（仅 verbose 有）。
4. 在图边写一句结论："`--verbose` 在这条链路上额外触发了哪几次 `readelf -aW`？"

**参考作答要点**（可对照源码核对）：

- `--verbose` 在融合产物路径上只额外触发**一次** `readelf -aW`：位于 [`_dump_elf_process` 的 AICORE_BINARY 分支](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/msobjdump/msobjdump/msobjdump_main.py#L348-L350)，对剥壳后的内层 ELF 打印全量信息。
- meta TLV 的打印（`.ascend.meta META INFO`、各 `RUNTIME_IMPLICIT_INFO`、`VERSION`、以及每个 kernel 的 `KERNEL_TYPE/CROSS_CORE_SYNC/MIX_TASK_RATION`）在 verbose 与否的情况下**都会出现**，链路是 `_show_elf_ascend_meta_obj` → `_get_elf_ascend_meta_op_tlv`（op 级，不截断+去重）+ `_get_elf_ascend_meta_tlv`（function 级）。
- 剥壳发生在**构造阶段**（`_extract_aicore_binary` 调 `llvm-objcopy`），早于 `_parse_process`，所以后续所有 `readelf` 都作用在剥壳后的内层 ELF 上。

## 6. 本讲小结

- `msobjdump` 命令经 `msobjdump.sh` 桥接到 `python -m msobjdump`，入口链是 `__main__.main` → `parse_args` → `run_obj_dump` → `ObjDump.run`；`entry_function` 用 `set_defaults` 注入，实现入口与实现解耦。
- `ObjDump` 的核心设计是"**命令模式 × 文件类型**"二维分流：构造阶段由 `_detect_obj_type`（两次 `readelf`）判定四种 `ObjType` 之一，执行阶段由 `_parse_process` 分发到 `_dump_elf_process` / `_extra_elf` / `_list_elf`。
- 融合编译产物（`.aicore_binary`）优先级最高，构造阶段用 `llvm-objcopy` 自动剥壳成内层 ELF 再继续解析；临时目录带 `pid` 支持多进程并发。
- 三种模式开销递减：dump 解析并打印、extract 落盘到 `out_dir`、list 仅打印文件名；单算子 ELF 无内嵌子文件，extract/list 直接 WARNING。
- **`--verbose` 只在 dump 模式额外追加 `readelf -aW` 全量 ELF 结构信息，不改变任何 meta TLV 字段的打印范围**——所有 meta 字段默认就打印。
- TLV 解析靠"4 字节头 `(type,length)` + 按 type 选 `struct.unpack` 格式 + 游标推进"循环；Function Meta（`.ascend.meta.<name>`）与 Binary Meta（`.ascend.meta`）两套，后者在融合产物下不截断且去重。

## 7. 下一步学习建议

本讲把 msobjdump 的"读"讲透了。接下来可以：

- **横向对比另一个 Python 解析工具**：进入第 7 单元，学习 `show_kernel_debug_data` 的 [`dump_parser.py`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/show_kernel_debug_data/show_kernel_debug_data/dump_parser.py)。它同样用 TLV 思路解析 dump bin 文件，但多了 magic 分发（FIFO vs workspace），可对比两者在"二进制格式驱动解析"上的异同。
- **回到 C++ 运行期视角**：msobjdump 是离线解析 ELF，而 [`cpudebug/include/kernel_elf_parser.h`](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kernel_elf_parser.h) 是运行期解析同一个 ELF。对比两者对 `.ascend.meta` TLV 的处理（C++ 只认 type=1/3，Python 全量打印），能加深对"同一产物、不同消费者"的理解。
- **动手扩展练习**：仿照 4.4.4 的示例，写一个小脚本，用 `readelf -SW` + `mmap` 把任意算子 ELF 的 `.ascend.meta` 段抽出来并按 TLV 循环打印，作为本讲的实战收尾。
