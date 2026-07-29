# 缓存机制与缓存目录结构

## 1. 本讲目标

编译一个 GPU 内核是慢操作（转译、多层 IR 优化、codegen、调 nvcc，动辄数秒到数十秒）。Tilus 用一套**基于内容哈希的磁盘缓存**把「同一个程序」的重复编译降到只做一次。本讲学完后，你应该能够：

- 说清一个程序是如何被映射到一个缓存目录的，缓存键（hash）由哪些成分计算而来。
- 画出 `programs/<hash>/` 与 `scripts/<name>/<hash>/` 两级缓存目录的内部结构，知道每个文件是什么。
- 解释一个常被踩的坑：**缓存键基于 Tilus IR 的文本，而不基于 codegen/emitter 的输出**，因此改了发射器后必须手动删缓存才能重新编译。
- 准确判断「什么改动会让缓存失效、什么改动不会」，并掌握清缓存的正确方式。

本讲承接 u3-1（`build_program` 六阶段流水线），把视角收窄到其中的「缓存」这一横切机制。

## 2. 前置知识

- **内容寻址缓存（content-addressable cache）**：用一个数据内容的摘要（哈希）作为它的存储地址/键名。内容不变 → 哈希不变 → 命中同一目录，从而跳过重复工作。
- **SHA256**：一种密码学哈希，把任意长度字节串映射成 64 位十六进制字符串；输入哪怕改一个比特，输出也面目全非。Tilus 取其前 12 位作为目录名。
- **Tilus IR 文本**：`str(prog)` 把一个 `Program` 渲染成人类可读、且确定性的文本（见 u3-3、u3-5）。它是缓存键的核心原料。
- **target**：编译目标架构（如 `sm80`/`sm90a`/`sm100a`，见 u1-2），同一个程序在不同 target 下产物不同，因此也是缓存键的一部分。
- **FileLock**：跨进程文件锁，避免两个进程同时编译同一个程序时互相踩踏。

一句话直觉：Tilus 把「这个程序长什么样 + 用什么选项编译」捏成一段文本，算个哈希当文件夹名；下次还是这段文本，就直接复用那个文件夹里的 `.so`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/drivers.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py) | 编译主编排：`get_cache_dir`（算缓存键、建目录）、`build_program`（命中检查 + 加锁编译）、`optimize_program`/`optimize_ir_module`（决定 `ir/` 与 `module/ir/` 落盘位置）。 |
| [python/tilus/option.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py) | 全局选项注册与读写：`cache_dir` 默认值解析、`debug.dump_ir`、`debug.disable_ptxas_opt` 等，并提供同名环境变量。 |
| [python/tilus/runtime/compiled_program.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/compiled_program.py) | 运行时加载：`compiled_program_exists` 用三件套判定缓存是否完整、`load_compiled_program` 加载 `lib.so`。 |
| [python/tilus/lang/instantiated_script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py) | 脚本级（script）缓存：建 `scripts/<name>/<hash>/`、用符号链接挂接各 schedule 的 program、写 dispatch 表。 |
| [docs/source/programming-guides/cache.rst](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/cache.rst) | 官方文档，给出缓存目录树与「安全删除」说明。 |
| [CLAUDE.md](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/CLAUDE.md) | 项目维护笔记，明确记录「键基于 IR 哈希而非 codegen 输出」这一关键陷阱。 |

---

## 4. 核心概念与源码讲解

### 4.1 缓存键的计算：SHA256(options + 程序文本)

#### 4.1.1 概念说明

「缓存键」要回答一个问题：**两次编译，输入是否完全相同？** 若相同就跳过、直接复用旧产物。

Tilus 的做法很直接：把所有可能影响产物*.so* 的输入捏成一段文本，对其算 SHA256，取前 12 个十六进制字符作目录名。于是「同一程序 + 同一编译选项」永远落到同一个 `programs/<hash>/` 目录。

需要注意，这里参与哈希的「程序」是**高层 Tilus IR 的文本表示**（`str(prog)`），而不是 codegen 出来的 `source.cu`。这个选择带来一个重要后果（见 4.3）：改动 codegen/emitter 不会改变缓存键。

#### 4.1.2 核心流程

`get_cache_dir(prog, options)` 的执行过程（伪代码）：

```
1. options_dict = asdict(BuildOptions)            # 目前只有 debug_block
2. options_dict += {disable_ptxas_opt, target}    # 补两个影响产物的全局选项
3. prog_text   = str(prog)                        # 高层 Tilus IR 的可读文本
4. options_text = str(options_dict)
5. hex_digest  = sha256(options_text + prog_text)[:12]
6. cache_dir   = <cache_dir>/programs/<hex_digest>/
7. 若该目录已存在 program.txt / options.txt：
        读取并比对内容 → 不一致就抛 ValueError（哈希碰撞/内容被篡改的守卫）
   否则：建目录，写入 program.txt 与 options.txt
8. 返回 cache_dir
```

哈希的输入成分可归纳为下表：

| 成分 | 来源 | 改动是否会改哈希 |
| --- | --- | --- |
| 程序文本 | `str(prog)`（Tilus IR） | 改了 IR（schedule/计算/dtype 等）→ 是 |
| `debug_block` | `BuildOptions` | 改调试块 → 是 |
| `disable_ptxas_opt` | `tilus.option.debug.disable_ptxas_opt` | 切换 → 是 |
| `target` | `tilus.target.get_current_target()` | 换架构 → 是 |

注意：**`source.cu` 的内容、emitter 的实现版本，都不在这张表里。**

#### 4.1.3 源码精读

`get_cache_dir` 用 `@functools.lru_cache(maxsize=1024)` 装饰，进程内对同一 `(prog, options)` 只算一次：

[python/tilus/drivers.py:L192-L224](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L192-L224) — 构造 `options_dict`（含 `disable_ptxas_opt`、`target`）、取 `prog_text = str(prog)`、把两段文本拼接后 `sha256(...)[:12]`，落到 `<cache_dir>/programs/<hex_digest>`。

`[:12]` 是截断：12 个十六进制字符 = 48 比特熵，对单个项目规模的程序数量足够区分；真发生碰撞时由下面的内容比对兜底。

[python/tilus/drivers.py:L225-L244](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L225-L244) — 写入 `program.txt` / `options.txt`；若二者已存在则读取比对，**内容不一致直接抛 `ValueError`**。这是一道防碰撞/防误改的守卫：保证「同名目录里的程序」与「当前要编译的程序」完全一致。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到缓存键是 `options_text + prog_text` 的 SHA256 前 12 位，并能复算。
2. **操作步骤**：
   - 准备一个能跑的内核（如 u1-3 的 `vector_add`）。
   - 开头设置一个独立缓存目录并开启 dump：
     ```python
     import tilus, hashlib
     tilus.option.cache_dir("./my-cache")
     tilus.option.debug.dump_ir(True)
     # ... 定义并调用一次 vector_add Script ...
     ```
   - 运行后，在 `./my-cache/programs/` 下会出现一个名为 12 位十六进制的子目录，记下它的名字（设为 `H`）。
   - 打开该目录里的 `program.txt` 和 `options.txt`，在 Python 里复算：
     ```python
     prog_text = open(f"./my-cache/programs/{H}/program.txt").read()
     options_text = open(f"./my-cache/programs/{H}/options.txt").read()
     print(hashlib.sha256(options_text.encode() + prog_text.encode()).hexdigest()[:12])
     ```
3. **需要观察的现象**：复算结果应等于目录名 `H`；`options.txt` 里应能看到 `disable_ptxas_opt` 与 `target` 两个字段。
4. **预期结果**：打印值与磁盘目录名完全一致，验证「目录名 = SHA256(options_text + prog_text)[:12]」。
5. 若本地无 GPU 或环境不完整：**待本地验证**；可改为只读 `drivers.get_cache_dir` 的源码核对算法。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `target` 从 `sm90a` 换成 `sm100a`，缓存会命中吗？
**答**：不会。`target` 进了 `options_dict` → `options_text` 变 → 哈希变 → 落到新的 `programs/<hash>/`，触发重新编译。

**练习 2**：`get_cache_dir` 上的 `lru_cache` 解决什么问题？能否省掉？
**答**：它让单次进程内对同一 `(prog, options)` 只做一次「算哈希 + 读写文件」的解析。例如 autotune 并行编译多个 schedule 时，同一 program 可能被反复查询。逻辑上可省（磁盘文件仍在），但会有重复 IO；它**不替代**磁盘缓存，磁盘命中与否由 `compiled_program_exists` 判断。

---

### 4.2 缓存目录结构：scripts 与 programs 两级组织

#### 4.2.1 概念说明

Tilus 的缓存目录分两层，对应两个抽象（u2-1、u8-l2 详述）：

- **program**：一份**具体的** Tilus IR 程序（某个 schedule 实例化后的结果），编译出一个 `.so`。
- **script**：一个**内核模板**（你的 `tilus.Script` 子类），带一个调优空间，可实例化出多份 program。

因此磁盘上也是两级：`programs/` 按 IR 哈希存「成品」，`scripts/` 按脚本名 + jit_key 存「调度空间、dispatch 表、指向各 program 的符号链接」。前者由 `drivers.get_cache_dir` 建，后者由 `InstantiatedScript`/`JitInstance` 建。

#### 4.2.2 核心流程：目录树

官方文档给出的完整结构（[docs/source/programming-guides/cache.rst:L62-L87](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/cache.rst#L62-L87)）：

```
cache/
├── scripts/                              # 每个 tilus 脚本一个入口
│   └── <script-name>/                    # 类名转 snake_case
│       └── <script-hash>/                # jit_key + 程序文本哈希
│           ├── programs/
│           │   ├── 0 -> ../../programs/… # 符号链接，指向第 0 个 schedule
│           │   ├── 1 -> ../../programs/… # 指向第 1 个 schedule
│           │   └── ...
│           └── dispatch_table.txt        # 动态输入尺寸 → 选哪个 program
│
└── programs/                             # 每个编译产物一个入口
    └── <program-hash>/                   # SHA256(program.txt + options.txt)[:12]
        ├── program.txt                   # 人类可读的 Tilus 程序
        ├── options.txt                   # 编译选项
        ├── ir/                           # 各 Tilus IR Pass 后的转储（dump_ir 开启时）
        └── module/
            ├── ir/                       # 各 Hidet IR Pass 后的转储（dump_ir 开启时）
            ├── source.cu                 # 生成的 CUDA 源码
            ├── compile.sh                # 实际调用 nvcc 的命令
            └── lib.so                    # 编译出的共享库
```

两个关键判断点：

1. **缓存是否「已完成」**：由 `compiled_program_exists` 用三件套判定——必须同时存在 `module/lib.so`、`program.txt`、`options.txt`。三者缺一即视为未完成，会重新编译。
2. **并发安全**：`build_program` 用 `filelock.FileLock(<dir>/.lock)` 串行化对同一目录的编译，并采用**双重检查**——拿到锁后再查一次 `compiled_program_exists`，避免多进程重复编译。

#### 4.2.3 源码精读

**program 级目录与命中判定**

[python/tilus/runtime/compiled_program.py:L65-L82](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/compiled_program.py#L65-L82) — `compiled_program_exists` 用 `all([... lib.so ... program.txt ... options.txt])` 判完成。这是整个缓存「命中/未命中」的最终裁决。

[python/tilus/drivers.py:L298-L309](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L298-L309) — `build_program`：先无锁查一次（命中即返回），再 `FileLock` 加锁，**锁内再查一次**（典型的 double-checked locking），都没命中才真正开始 `verify → optimize → ...` 编译。

[python/tilus/runtime/compiled_program.py:L35-L45](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/compiled_program.py#L35-L45) — 命中后 `load_compiled_program` 直接 `tvm_ffi.load_module(<dir>/module/lib.so)` 取出 `launch` 函数，零编译。

**IR 落盘位置（dump_ir 开启时）**

[python/tilus/drivers.py:L89-L93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L89-L93) — Tilus IR 各 Pass 后的转储落到 `cache_dir / "ir"`；[python/tilus/drivers.py:L183-L189](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L183-L189) — Hidet IR 各 Pass 后的转储落到 `cache_dir / "module" / "ir"`。这正是目录树里 `ir/` 与 `module/ir/` 的来源。

**compile.sh 的来源**：CUDA 源码经 `compile_source` 调用 nvcc，其间会把编译命令写进 `compile.sh`（[python/tilus/hidet/backend/build.py:L77-L78](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/backend/build.py#L77-L78)），便于离线复现编译。

**script 级目录**

[python/tilus/lang/instantiated_script.py:L517-L529](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L517-L529) — 把所有 schedule 的程序文本拼起来再算一个 `[:8]` 哈希，与 `jit_key` 拼成 `scripts/<snake_case 名>/<key-hash>/` 并 `mkdir`。

[python/tilus/lang/instantiated_script.py:L647-L656](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L647-L656) — 在该目录下建 `programs/` 子目录，用符号链接 `0, 1, ...` 指向各 schedule 实际的 `programs/<hash>` 目录（即 script 级只是「指路」，真正的 `.so` 仍在 `programs/` 下）。

[python/tilus/lang/instantiated_script.py:L787-L804](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L787-L804) — `dump_dispatch_table` 同时写 `dispatch_table.json`（含 `collect_tuning_metadata()` 的环境指纹）与人类可读的 `dispatch_table.txt`（每行：调优键 → 选中的 program 编号）。

#### 4.2.4 代码实践

1. **实践目标**：用一个带 `@autotune`（多 schedule）的内核，同时看到 `scripts/` 与 `programs/` 两级结构，并理解符号链接的指向。
2. **操作步骤**：
   - 复用 u2-4 的 autotune matmul（`block_m/block_n/block_k` 各给 2 个候选）。
   - `tilus.option.cache_dir("./my-cache")` 后运行一次。
   - 在 `./my-cache/scripts/matmul_v0/.../programs/` 下查看 `0`、`1` 等符号链接，用 `readlink -f ./my-cache/scripts/.../programs/0` 看它们指向 `programs/` 下的哪个哈希目录。
   - 打开 `dispatch_table.txt`，确认某组输入尺寸被映射到某个 program 编号。
3. **需要观察的现象**：`scripts/.../programs/<n>` 是符号链接而非真实目录；其目标都在顶层 `programs/` 下；`programs/<hash>/module/lib.so` 是真实产物。
4. **预期结果**：`scripts/` 只存调度与指路信息（文本 + 链接 + dispatch 表），`programs/` 才存编译产物，两者解耦——同一份 program 可被多个 script 复用。
5. 无 GPU 环境：**待本地验证**；可改为只读目录结构，对照源码理解。

#### 4.2.5 小练习与答案

**练习 1**：`compiled_program_exists` 为什么要同时检查 `lib.so`、`program.txt`、`options.txt` 三个文件，而不是只看 `lib.so`？
**答**：防止「编译到一半被打断」的残缺缓存被误用。比如 nvcc 崩溃可能留下不完整的 `lib.so`；要求三者齐全才算成功。`program.txt`/`options.txt` 还兼作碰撞守卫（4.1.3）的内容比对来源。

**练习 2**：同一个 script 的两个不同 schedule，它们的 `.so` 会落在同一个 `programs/<hash>/` 吗？
**答**：不会。不同 schedule 产生不同的 Tilus IR → 不同的 `str(prog)` → 不同的哈希 → 各自独立的 `programs/<hash>/`。script 级目录用符号链接把它们组织起来。

---

### 4.3 缓存键基于 IR 而非 codegen：何时需要手动清缓存

#### 4.3.1 概念说明

这是本讲最重要的结论，也是 CLAUDE.md 明确记录的陷阱：

> 缓存键基于 **Tilus IR 的哈希**，而不是 codegen 输出。对 emitter/codegen 的改动（例如修正地址计算）**不会**让已缓存的 program 失效。改完 emitter 后必须删掉缓存目录（`.cache`、`.cache/.test_cache` 或脚本专属缓存）才能强制重编译。

直觉解释：缓存回答的是「输入变了吗」。Tilus 认为「输入」=`Tilus IR + 选项`，而 emitter 是「编译器的实现」——它不在这套输入里。于是改 emitter 等于换了「另一版编译器」，但缓存键感知不到这版差异，于是把**旧版编译器产出的 `.so`** 当成仍然有效直接复用。

这对终端用户通常无所谓（他们不改 emitter）；但对** Tilus 的开发者**是个高频坑：明明改了发射器、修了地址 bug，跑出来行为却没变——因为用的还是旧 `.so`。

#### 4.3.2 核心流程：缓存何时失效、何时不失效

判定准则只看一件事——**改动是否改变了 `str(prog)`（Tilus IR 文本）或 `options_text`**：

| 改动类型 | 是否改变 IR/选项 | 缓存是否失效 |
| --- | --- | --- |
| 改内核里的**注释/空白** | 否（注释不进 AST → 不进 IR） | **否**（复用旧 `.so`） |
| 重构 `__call__` 的 Python 写法但生成相同 IR | 否 | **否** |
| 改 schedule（`block_m` 等分块参数） | 是 | 是（新哈希） |
| 改计算逻辑 / dtype / `num_warps` | 是 | 是 |
| 换 `target`（如 sm80→sm90a） | 是（改 `options`） | 是 |
| 切换 `debug.disable_ptxas_opt` | 是（改 `options`） | 是 |
| **改 emitter / codegen 实现** | **否**（IR 不变） | **否 ⚠️ 但产物本应变** |
| 改某个 Pass 的变换逻辑 | 视是否改变最终 IR 而定 | 多数**否 ⚠️** |

清除缓存的正确方式（任选其一，均安全）：

1. 直接删整个缓存目录：`rm -rf .cache`（或你设置的 `cache_dir`）。
2. 指向一个全新的空目录：`tilus.option.cache_dir("./fresh-cache")`——适合只想丢弃当前编译又想保留旧产物对照时。
3. 只删受影响的单个 `programs/<hash>/` 目录——精确但需要先定位哈希。

官方文档同样确认「缓存目录可随时安全删除，下次运行自动重编译」（[docs/source/programming-guides/cache.rst:L15-L18](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/docs/source/programming-guides/cache.rst#L15-L18)）。

#### 4.3.3 源码精读

`get_cache_dir` 里参与哈希的只有 `prog_text` 与 `options_text`，全程不读 `source.cu`、不知道 emitter 版本：

[python/tilus/drivers.py:L221-L224](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L221-L224) — `prog_text = str(prog)`、`hex_digest = sha256(options_text + prog_text)[:12]`。**没有任何 codegen 产物或 emitter 标识进入哈希**，这就是「改 emitter 不失效」的根因。

CLAUDE.md 把这条作为开发须知直接写明：

[CLAUDE.md](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/CLAUDE.md)（Cache 节）— 明确「The cache key is based on the Tilus IR hash, not the codegen output」，并要求改 emitter 后删 `.cache` / `.cache/.test_cache` / 脚本专属缓存以强制重编译。

默认缓存目录解析（决定你要删哪里）：

[python/tilus/option.py:L24-L48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L24-L48) — `_get_default_cache_dir`：若 Tilus 源码位于 git 仓库内，默认落在**仓库根的 `.cache/`**；否则落在 `~/.cache/tilus`。所以从源码开发 Tilus 时，缓存就在你眼皮底下的 `.cache/`。

[python/tilus/option.py:L53-L59](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L53-L59) 与 [python/tilus/option.py:L114-L123](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L114-L123) — `cache_dir` 选项（环境变量 `TILUS_CACHE_DIR`）及其 setter，用于把缓存重定向到你指定的位置，方便隔离与清理。

#### 4.3.4 代码实践

本实践对应本讲任务：验证「codegen 无关的改动不触发重编译」，并归纳失效边界。

1. **实践目标**：证明改注释不换缓存键、不重编译；进而说清什么改动才会让缓存失效。
2. **操作步骤**：
   - 第 1 次运行：用 u1-3 的 `vector_add`，`tilus.option.cache_dir("./exp-cache")`，记录 `./exp-cache/programs/` 下目录名 `H`，并记录运行耗时（首次会编译）。
   - 在 `vector_add.py` 的 `__call__` 里**加一行注释**（如 `# demo: cache reuse`），保存。
   - 第 2 次运行（同样的 `n`）：再查 `./exp-cache/programs/` 下的目录名，对比是否还是 `H`；记录耗时（应显著变快，因为跳过编译）。
   - 把那行注释**改成一个真正影响 IR 的改动**：例如把 `c = ra + rb` 改成 `c = ra * 2.0 + rb`，保存。
   - 第 3 次运行：再查目录名，应出现一个**新的** `H'`（旧 `H` 仍在），且耗时回到「需要编译」的水平。
3. **需要观察的现象**：
   - 第 2 次：目录名不变、无新 `module/source.cu` 生成时间更新、运行更快 → **缓存复用**。
   - 第 3 次：出现新哈希目录、其下有新生成的 `source.cu` / `lib.so` → **缓存失效并重编译**。
4. **预期结果**：注释改动复用缓存（因 `str(prog)` 不变）；计算改动产生新缓存目录。由此可总结：**只有改变 Tilus IR 文本或 `options` 的改动才会失效缓存**；改 emitter/codegen/Pure-Python 写法（产出相同 IR）都不会。
5. 进阶（开发者向，**待本地验证**）：若你在改 Tilus 的 emitter 源码，把第 2 步的「改注释」换成「改某发射器实现」，会观察到和改注释**一样**——缓存不失效、用的还是旧 `.so`。这正是必须手动 `rm -rf ./exp-cache` 的场景。
6. 无 GPU 环境：**待本地验证**；可退化为源码阅读型实践——对照 4.3.3 的两段源码，确认哈希输入里确实没有 codegen 成分，从而逻辑推出结论。

#### 4.3.5 小练习与答案

**练习 1**：你修了 `python/tilus/backends/emitters/ldst.py` 里的一个地址计算 bug，重新跑内核却发现输出没变化。最可能的原因和最快的修复是什么？
**答**：缓存键不含 emitter 版本，旧 `.so` 被原样复用。修复：删掉缓存目录（`rm -rf .cache` 或你设的 `cache_dir`，开发时多为仓库内 `.cache/`）后再跑，强制走完整编译。

**练习 2**：把 `tilus.option.debug.disable_ptxas_opt(True)` 打开后再跑同一个内核，会复用旧缓存吗？
**答**：不会。`disable_ptxas_opt` 进了 `options_dict`（见 4.1.3），`options_text` 改变 → 哈希改变 → 新缓存目录。这也解释了为何调试 PTX 时不会污染正常缓存。

**练习 3**：为什么 Tilus 不把「emitter/编译器版本」加进缓存键来自动避免这个坑？
**答**：参考性答案——加进去需要可靠地追踪每一处影响 codegen 的代码版本（emitter、各 Pass、hidet 后端），成本高且易漏；当前设计优先保证「输入决定输出」的纯粹性与命中稳定性，把 emitter 变更视为低频的开发期事件，用「手动清缓存」这条简单规则来兜底（如需自动失效，可在开发期把 `cache_dir` 指到一次性目录）。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「定位—解释—清缓存」的完整排障：

**场景**：你正在开发 Tilus，改了某个通用发射器（见 u6-4），需要确认改动真的生效。

1. **定位缓存**：写一个最小内核，`tilus.option.cache_dir("./diag-cache")` 后运行。在 `./diag-cache/programs/<H>/module/source.cu` 里找到受你改动影响的那段 CUDA 代码，确认它仍是**旧**实现（证明缓存被复用、改动未生效）。
2. **解释根因**：用 4.1 的算法说明——你的改动没碰 `str(prog)` 也没碰 `options`，故 `H` 不变，`compiled_program_exists` 命中旧 `lib.so`。
3. **强制失效**：执行 `rm -rf ./diag-cache`（或改用 `tilus.option.cache_dir("./diag-cache2")`）后重跑，再次查看新生成 `<H'>/module/source.cu`，确认这次反映了你的 emitter 改动。
4. **佐证**：对照 `program.txt`（两次应基本一致，因 IR 没变）与 `options.txt`（也应一致），从而直观体会「IR 没变、产物却该变」正是需要手动清缓存的唯一信号。

预期：第 1 步看到旧行为，第 3 步看到新行为，`program.txt`/`options.txt` 在两次间几乎不变——这组对照就是你理解「键基于 IR 而非 codegen」的实证。（无 GPU 环境则**待本地验证**，可退化为对照源码的逻辑推演。）

## 6. 本讲小结

- 缓存键 = `sha256(options_text + str(prog))[:12]`，其中 `options_text` 含 `BuildOptions.debug_block`、`disable_ptxas_opt`、`target`；键落在 `<cache_dir>/programs/<hash>/`。
- 缓存目录分两级：`programs/<hash>/` 存真实编译产物（`program.txt`/`options.txt`/`ir/`/`module/{ir,source.cu,compile.sh,lib.so}`），`scripts/<name>/<hash>/` 存调度空间、dispatch 表与指向各 program 的符号链接。
- 命中判定由 `compiled_program_exists` 的「`lib.so`+`program.txt`+`options.txt`」三件套裁决；`build_program` 用 `FileLock` 双重检查保证多进程安全。
- **键基于 Tilus IR 文本，不含 codegen/emitter 输出**：改注释/纯 Python 写法（产出相同 IR）不失效；改 schedule/计算/dtype/target/disable_ptxas_opt 才失效。
- 改 emitter/codegen 后**缓存不会自动失效**，必须手动删缓存目录（开发期通常是仓库内 `.cache/`）或换一个新 `cache_dir` 才能强制重编译。
- 缓存目录可随时安全删除，下次运行自动重编译。

## 7. 下一步学习建议

- 继续沿 u8 单元阅读 **u8-l2（自动调优 dispatch 缓存）**：本讲的 `programs/` 是「程序级」缓存，u8-l2 讲 `dispatch_table` 与 `collect_tuning_metadata` 的「环境指纹」如何让调优结果跨机器自动失效，是本讲 script 级缓存的自然延伸。
- 阅读 **u8-l3（运行时 CompiledProgram）**：看 `load_compiled_program` 如何把本讲缓存的 `lib.so` 加载成可调用对象，串起「命中 → 加载 → launch」的最后一公里。
- 若想理解 `ir/` 与 `module/ir/` 里那些转储文件的来源，回顾 **u3-1（build_program 全流程）** 与 **u5-2（默认变换流水线）**，对照各 Pass 名称阅读。
- 开发 Tilus 自身时，结合 **u8-l4（调试与剖析）** 的 `dump_ir` / `disable_ptxas_opt` 工作流，把本讲的「清缓存 → 重编译 → 看产物」养成肌肉记忆。
