# JIT 磁盘缓存

## 1. 本讲目标

在 u7-l3 中我们看到：cubin 的生成要把字节码写临时文件、再 fork 一个 `tileiras` 子进程来编译。这是整条编译流水线里最贵的一步——要起进程、要跑完整的后端优化与代码生成。如果一个内核的「源码 + 编译选项 + 目标架构」没变，下次启动时再编译一遍显然是浪费。

本讲讲解 cuTile 的解决方案：**基于 SQLite 的 cubin 磁盘缓存**。学完后你应该能够：

- 说清缓存键 `cache_key` 由哪些字段哈希而成，为什么这些字段一个都不能少；
- 画出 `cache` 表的结构（`key / blob / blob_size / atime` 四列）以及读写流程；
- 解释 `evict_lru` 的「按 `atime` 排序 + 累计窗口大小」淘汰策略，并算出给定场景下哪些条目会留下；
- 知道 `CUDA_TILE_CACHE_DIR` 与 `CUDA_TILE_CACHE_SIZE` 两个环境变量的取值语义与默认值；
- 能用 `test/test_cache.py` 里的单元测试独立验证缓存行为，而不必每次都拉起真实 GPU。

## 2. 前置知识

- **cubin**：GPU 可执行二进制（compile product），由后端编译器 `tileiras` 从 TileIR 字节码编译而来（见 u7-l3）。
- **字节码（bytecode）**：树形 Tile IR 被压扁后的线性二进制（见 u7-l1 / u7-l2）。它是「内核长什么样」的最终编码，因此也是缓存键的核心输入。
- **SQLite**：一个嵌在单文件里的轻量关系数据库，cuTile 用 Python 标准库 `sqlite3` 直接驱动，无需额外服务进程。
- **LRU（Least Recently Used）**：一类缓存淘汰策略——空间不够时优先丢弃「最久没被用过」的条目。cuTile 的实现略有变形，下文会专门讲。
- **SHA-256**：一种密码学哈希函数，把任意长度输入压成固定 256 位的指纹；cuTile 用它的十六进制串当缓存主键。
- **`TileContext` / `TileContextConfig`**：cuTile 运行时的全局配置对象，缓存目录与配额就挂在上面的 `config` 字段里（见 u1-l3、u8-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/cuda/tile/_cache.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cache.py) | 缓存的全部实现：建表、连接、`cache_key`、`cache_lookup`、`cache_store`、`evict_lru`。本讲的主角。 |
| [src/cuda/tile/_context.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py) | `TileContextConfig` 数据类与从环境变量解析配置的一组函数，缓存目录/配额来源于此。 |
| [src/cuda/tile/_compile.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) | `compile_tile` 在生成 cubin 前后调用缓存：先 `cache_lookup` 命中即跳过 `tileiras`，未命中则编译后 `cache_store` + `evict_lru`。 |
| [test/test_cache.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_cache.py) | 缓存的单元测试，是本讲「无 GPU 也能做」的代码实践主战场。 |

## 4. 核心概念与源码讲解

本讲把缓存拆成四个最小模块：**配置来源（`TileContextConfig`）→ 缓存键（`cache_key`）→ 读写（`cache_lookup` / `cache_store`，含 SQLite 表与连接管理）→ 淘汰（`evict_lru`）**。这四者正好对应缓存生命周期的四个阶段。

### 4.1 TileContextConfig：缓存配置从哪里来

#### 4.1.1 概念说明

缓存需要两个外部参数才能运转：

1. **缓存目录**：SQLite 文件 `cache.db` 放在哪个目录；为 `None` 表示完全禁用缓存。
2. **配额**：缓存最多占多少字节，超出就要淘汰旧条目。

这两个值不是硬编码的，而是由用户通过环境变量配置、在 cuTile 初始化时一次性解析进 `TileContextConfig`，再挂到全局 `default_tile_context` 上供 `compile_tile` 读取。把「配置解析」与「缓存实现」分到 `_context.py` 与 `_cache.py` 两个文件，是典型的「策略与机制分离」。

#### 4.1.2 核心流程

```
进程启动
  └─ cext 在构造 default_tile_context 时
       └─ 调用 init_context_config_from_env()
            ├─ get_cache_dir_from_env()        → 读 CUDA_TILE_CACHE_DIR
            └─ get_cache_size_limit_from_env() → 读 CUDA_TILE_CACHE_SIZE
       └─ 得到 TileContextConfig(cache_dir=..., cache_size_limit=...)
  └─ 之后 compile_tile 通过 context.config.cache_dir / cache_size_limit 取用
```

#### 4.1.3 源码精读

`TileContextConfig` 是一个普通 `dataclass`，缓存相关的两个字段是 `cache_dir` 与 `cache_size_limit`：

[src/cuda/tile/_context.py:15-23](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L15-L23) —— 注意 `cache_dir` 是 `Optional[str]`，`None` 即「禁用缓存」；`cache_size_limit` 是 `int`（字节）。

解析入口把环境变量逐一映射成字段：

[src/cuda/tile/_context.py:26-35](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L26-L35) —— `init_context_config_from_env` 在 C++ 扩展构造 `default_tile_context` 时被调用（见 `cext/tile_kernel.cpp` 里的 `PyObject_CallMethod(..., "init_context_config_from_env", "")`），所以这两个环境变量必须在 `import cuda.tile` **之前**设置好。

缓存目录的解析逻辑值得细看，它有「默认值 + 平台差异 + 禁用哨兵」三层：

[src/cuda/tile/_context.py:91-101](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L91-L101) —— 三层含义：

- 默认目录：Linux/macOS 用 `$XDG_CACHE_HOME`，没设就退回 `~/.cache`；Windows 用 `%LOCALAPPDATA%`。最终都拼上 `cutile-python` 子目录（即默认 `~/.cache/cutile-python`）。
- 自定义：设 `CUDA_TILE_CACHE_DIR=/some/path` 即可改到任意位置。
- **禁用**：把 `CUDA_TILE_CACHE_DIR` 设成 `0` / `off` / `none` / 空串，函数返回 `None`，缓存被整体关闭。

配额的解析则一行搞定，默认 2 GiB：

[src/cuda/tile/_context.py:104-105](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_context.py#L104-L105) —— `1 << 31` 即 2147483648 字节 = 2 GiB。设 `CUDA_TILE_CACHE_SIZE=<字节数>` 可改。

#### 4.1.4 代码实践

1. **实践目标**：看清你自己机器上缓存默认落在哪、配额多大，并验证禁用哨兵生效。
2. **操作步骤**：
   ```bash
   # (a) 默认行为：什么都不设
   python -c "import cuda.tile as ct; \
     from cuda.tile._cext import default_tile_context; \
     print('dir =', default_tile_context.config.cache_dir); \
     print('limit =', default_tile_context.config.cache_size_limit)"
   ```
   ```bash
   # (b) 禁用缓存
   CUDA_TILE_CACHE_DIR=off python -c "import cuda.tile as ct; \
     from cuda.tile._cext import default_tile_context; \
     print('dir =', default_tile_context.config.cache_dir)"
   ```
3. **需要观察的现象**：(a) 应打印形如 `dir = /home/<you>/.cache/cutile-python`、`limit = 2147483648`；(b) 应打印 `dir = None`。
4. **预期结果**：与文档 `docs/source/debugging.rst` 里「Defaults to `~/.cache/cutile-python`」「Defaults to 2 GB」一致。
5. 若你的环境变量未生效，请确认是在启动 Python 进程之前（而非 `import` 之后用 `os.environ` 改）设置的——配置只在构造 `default_tile_context` 时读一次。

#### 4.1.5 小练习与答案

**练习**：把 `CUDA_TILE_CACHE_DIR` 设成空字符串 `""` 与根本不设，行为有区别吗？

**答案**：有区别。根本不设时走默认值 `~/.cache/cutile-python`（缓存开启）；设成 `""` 时命中 `get_cache_dir_from_env` 里的 `env.strip().lower() in ("0","off","none","")` 判断，返回 `None`，缓存被关闭。

---

### 4.2 cache_key：把编译上下文哈希成缓存键

#### 4.2.1 概念说明

缓存的「主键」必须满足一个硬约束：**两个内核只有当「编译出来的 cubin 必然完全相同」时，才允许共享同一个键**。否则要么误命中（拿到错的 cubin，灾难性 bug），要么漏命中（白编译一次，只是慢）。

要做到这点，键必须覆盖所有会影响 cubin 的输入。cuTile 的选择是把这些输入按固定顺序拼成字节流，再做 SHA-256，用十六进制摘要当主键。任何一项不同，摘要就天差地别（雪崩效应）。

#### 4.2.2 核心流程

`cache_key` 的输入共有五项：

| 输入 | 含义 | 改变它的典型场景 |
| --- | --- | --- |
| `compiler_version` | `tileiras` 的版本串 | 升级了 `tileiras` pip 包或 CUDA Toolkit |
| `sm_arch` | 目标架构，如 `sm_90` | 换了一块 GPU |
| `opt_level` | 优化等级（实际生效值） | 改 `CompilerOptions.opt_level` |
| `device_debug` | 是否带 `--device-debug`（-O0） | 设了 `EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD` |
| `bytecode` | 完整 TileIR 字节码 | 内核源码、tile 尺寸（`Constant`）、静态形状、调用约定任何变化 |

外加一个内置的 `_CACHE_VERSION`（当前为空 `b''`），是维护者预留的「全局失效开关」：将来若缓存格式或语义改了，改这个常量就能让所有旧缓存键瞬间失效，强制全量重编译。

把 `opt_level` 与 `device_debug` 打包进同一个 32 位整数的做法很巧妙：

\[
\text{flags} = \text{opt\_level}\ \ |\ \ (\text{int}(\text{device\_debug}) \ll 8)
\]

`opt_level` 占低几位（取值 0~3），`device_debug` 占第 8 位，两者正交、互不干扰，省掉一次额外的长度前缀。

最终键 = `sha256( _CACHE_VERSION ‖ len(version) ‖ version ‖ len(arch) ‖ arch ‖ flags ‖ len(bytecode) ‖ bytecode )` 的十六进制。每个字符串/字节段都带「4 字节大端长度前缀」，是为了消除拼接歧义（否则 `"ab"+"c"` 与 `"a"+"bc"` 会哈希出同一个值）。

#### 4.2.3 源码精读

[src/cuda/tile/_cache.py:58-79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cache.py#L58-L79) —— `_CACHE_VERSION` 当前是 `b''`；`cache_key` 内部定义局部函数 `encode_uint` 把整数编成 4 字节大端，然后按固定顺序 `h.update(...)` 喂给 SHA-256，最后返回 `hexdigest()`。

注意键里用的是「**实际生效**」的 `opt_level` 与 `device_debug`，而不是用户写在 `CompilerOptions` 里的值。这层归一在 `compile_tile` 里完成：

[src/cuda/tile/_compile.py:569-578](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L569-L578) —— 当 `EXPERIMENTAL_CUDA_TILE_DEBUG_BUILD` 打开时，无论用户怎么设，`tileiras` 都会被强制成 `-O0 --device-debug`；因此键也必须用 `0, True` 来算，否则 debug 构建会和 release 构建的 cubin 撞键。这正是函数 docstring 里强调「the disk cache key must match」的原因。

`compiler_version` 来自一个被 `@cache` 的探测函数：

[src/cuda/tile/_compile.py:794-798](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L794-L798) —— 它去问 `tileiras` 自己的版本号。如果探测失败返回 `None`，`compile_tile` 会**主动禁用缓存**并打 warning（见 4.4.3 引用处的 `elif compiler_ver is None` 分支），宁可不缓存也绝不用版本不明的键。

#### 4.2.4 代码实践

1. **实践目标**：用单元测试验证「任一输入变化，键就变化」，无需 GPU。
2. **操作步骤**：直接读测试，再自己改一改跑：
   ```bash
   cd <仓库根目录>
   python -m pytest test/test_cache.py::test_cache_key_equal test/test_cache.py::test_cache_key_differs test/test_cache.py::test_cache_key_device_debug_differs -v
   ```
   再手算一下：
   ```python
   # 示例代码：可独立运行，无需 GPU
   from cuda.tile._cache import cache_key
   base = cache_key("v1", "sm_90", 3, b"data")
   print("opt=2 ->", cache_key("v1", "sm_90", 2, b"data") != base)   # True
   print("debug ->", cache_key("v1", "sm_90", 3, b"data", True) != base)  # True
   ```
3. **需要观察的现象**：四个断言（换 compiler_version / sm_arch / opt_level / bytecode）全部为真；device_debug 翻转也改变键。
4. **预期结果**：与 [test/test_cache.py:19-29](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_cache.py#L19-L29) 的断言一致——任何一项不同，键必不同。
5. 本实践是纯函数调用，结果确定，无需「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么长度前缀（`encode_uint(len(version))`）不能省？

**答案**：没有长度前缀的话，`version="a", arch="bc"` 与 `version="ab", arch="c"` 拼出的字节流都是 `"abc"`，会哈希出同一个键，造成误命中。长度前缀让每段边界明确，消除歧义。

**练习 2**：把内核的 `TILE_SIZE` 从 `Constant[int]` 的 16 改成 32，会改变 `cache_key` 吗？

**答案**：会。`Constant` 参数在编译期被烘焙进字节码（见 u3-l5），`TILE_SIZE` 取值不同 → 字节码不同 → `bytecode` 输入不同 → 键不同 → 编译出独立的 cubin。这也是 `Constant` 取值组合过多会引发「编译爆炸」的根因之一：每个取值都占一个缓存槽。

---

### 4.3 SQLite 表结构、连接管理与 cache_lookup / cache_store

#### 4.3.1 概念说明

cubin 是二进制大块数据（动辄几十 KiB），需要按「键 → 二进制」存取，并支持「按访问时间排序」「按大小求和」用于淘汰。SQLite 的单文件关系表正好胜任，且 Python 标准库自带驱动。

cuTile 的表结构极简，只有四列：

| 列 | 类型 | 含义 |
| --- | --- | --- |
| `key` | `TEXT PRIMARY KEY` | `cache_key` 算出的 SHA-256 十六进制串 |
| `blob` | `BLOB NOT NULL` | cubin 原始字节 |
| `blob_size` | `INTEGER NOT NULL` | `len(cubin)`，冗余存一份便于 `SUM` 求和 |
| `atime` | `REAL NOT NULL` | 最近一次访问的 Unix 时间戳（access time） |

`atime` 是实现 LRU 的关键：每次命中都刷新它，淘汰时按它从小到大（最旧优先）删。

#### 4.3.2 核心流程

**写入（`cache_store`）**：

```
cache_store(cache_dir, key, cubin)
  └─ _connect(cache_dir)            # 不存在则建目录、打开/建表 cache.db
  └─ INSERT OR IGNORE (key, blob, blob_size, atime=now)
  └─ commit / close
```

`INSERT OR IGNORE` 的语义很重要：若键已存在，**什么都不做**——既不覆盖 blob，也不刷新 atime。换言之「重复编译同一个内核」不会更新缓存条目。

**读取（`cache_lookup`）**：

```
cache_lookup(cache_dir, key)
  └─ _connect(cache_dir)
  └─ SELECT blob WHERE key=?        # 未命中返回 None
  └─ UPDATE atime=now WHERE key=?   # 命中才刷新访问时间
  └─ commit / close → 返回 blob
```

注意刷新 `atime` 发生在**命中之后**，这正是 LRU「用到就续命」的语义。

**连接与自愈（`_connect` / `_open_db`）**：

```
_connect(cache_dir)
  └─ makedirs(cache_dir, exist_ok=True)
  └─ db_path = cache_dir/cache.db
  └─ try: _open_db(db_path)          # 建表、建索引
     except sqlite3.Error:           # 数据库损坏
        unlink(db_path)              # 删掉坏文件
        _open_db(db_path)            # 重建一个空的
```

缓存损坏时**静默自愈**：删掉旧库重建空库，最坏不过是丢了缓存、下次重编译，绝不让缓存故障打断用户程序。所有公开函数（lookup/store/evict）都用 `try/except (sqlite3.Error, OSError)` 兜底，失败仅 `logger.debug` 记录后返回 `None`（lookup）或静默（store/evict）——缓存永远是「尽力而为」的优化，不承担正确性责任。

#### 4.3.3 源码精读

表结构定义在模块顶部：

[src/cuda/tile/_cache.py:14-21](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cache.py#L14-L21) —— 四列，`key` 为主键。`_CACHE_FILENAME = "cache.db"` 在 [第 23 行](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cache.py#L23)。

`_open_db` 建表之外还维护两个索引（并顺手丢弃一个旧名索引，做兼容迁移）：

[src/cuda/tile/_cache.py:34-40](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cache.py#L34-L40) —— `idx_cache_atime_key on cache(atime, key)` 服务淘汰时的 `ORDER BY atime, key`；`idx_cache_blob_size on cache(blob_size)` 服务 `SUM(blob_size)` 聚合。

连接与自愈：

[src/cuda/tile/_cache.py:43-55](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cache.py#L43-L55) —— 损坏时 `os.unlink` 后重开。

`cache_lookup`：

[src/cuda/tile/_cache.py:82-102](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cache.py#L82-L102) —— 命中后 `UPDATE atime` 再 `commit`，失败兜底返回 `None`。

`cache_store`：

[src/cuda/tile/_cache.py:105-118](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cache.py#L105-L118) —— `INSERT OR IGNORE`，`atime` 用 `time.time()`。

最后看 `compile_tile` 把它们串起来的现场：

[src/cuda/tile/_compile.py:522-564](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L522-L564) —— 流程是：取 `cache_dir` → 取 `compiler_ver`（为 `None` 则禁用并 warning）→ 算 `effective_opt, device_debug` → 算 `key` → `cache_lookup`：命中就直接把 cubin 塞进 `ret` 提前 `return`，**完全跳过 `tileiras`**；未命中才写临时字节码、调 `compile_cubin`、读回 cubin，最后 `cache_store` + `evict_lru`。这段代码清楚地回答了「缓存到底省了什么」：省的是 `compile_cubin` 那次子进程调用。

#### 4.3.4 代码实践

1. **实践目标**：用单元测试验证「写入后能读出同一份 cubin」「未命中返回 `None`」「命中会刷新 atime」，全程不需要 GPU 或 `tileiras`。
2. **操作步骤**：
   ```bash
   python -m pytest test/test_cache.py::test_store_then_lookup \
                     test/test_cache.py::test_lookup_miss \
                     test/test_cache.py::test_lookup_updates_atime -v
   ```
   也可以手写一小段（示例代码，无需 GPU）：
   ```python
   import time, sqlite3, os
   from cuda.tile._cache import cache_key, cache_store, cache_lookup
   d = "/tmp/cutile_demo_cache"
   k = cache_key("v1", "sm_90", 3, b"data")
   cache_store(d, k, b"\x7fELF_fake_cubin")
   assert cache_lookup(d, k) == b"\x7fELF_fake_cubin"   # 命中
   assert cache_lookup(d, "z"*64) is None               # 未命中
   ```
3. **需要观察的现象**：`test_lookup_updates_atime` 先把某条目的 `atime` 手动改成 1000 秒前，再 `cache_lookup` 一次，断言数据库里的 `atime` 已被刷新成「现在」。
4. **预期结果**：三个测试全部通过；与 [test/test_cache.py:38-79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_cache.py#L38-L79) 的断言一致。
5. 想直观看库文件，可在运行后 `sqlite3 /tmp/cutile_demo_cache/cache.db ".schema cache"` 看到那张四列表。

#### 4.3.5 小练习与答案

**练习**：`cache_store` 用 `INSERT OR IGNORE`。假设同一个键对应的 cubin 在不同版本 `tileiras` 间**理论上**会变，为什么这里仍安全地选择「已存在就忽略」？

**答案**：因为 `cache_key` 已经把 `compiler_version` 编进了键。换版本会改变 `compiler_version` → 改变键 → 根本不会撞到旧条目，而是插一条新的。所以在「同键」前提下，cubin 必然相同，`IGNORE` 是安全且高效的（避免无谓覆盖写）。

---

### 4.4 evict_lru：基于 atime 与累计窗口大小的 LRU 淘汰

#### 4.4.1 概念说明

配额（默认 2 GiB）不是软建议——`compile_tile` 每次写入新 cubin 后都会调一次 `evict_lru(cache_dir, cache_size_limit)`，把总大小压回配额附近。

cuTile 的淘汰策略可以概括为「**按 `atime` 从旧到新排序，删除累计字节数落入「超出配额」窗口的最旧条目**」。它不是「每次溢出就删到略低于配额」，而是「保留最近用到、合计约等于配额的那一段，把更旧的整段删掉」。这就是大纲里说的「atime + 窗口累计大小」式 LRU。

#### 4.4.2 核心流程

设当前缓存总大小为 \(T\)、配额为 \(L\)（仅当 \(T > L\) 时才需要淘汰）。

1. 把所有条目按 `(atime, key)` 升序排列（最旧的在最前）。
2. 对这个序列做 `blob_size` 的**前缀和**（running cumulative size）\(C_i\)。
3. 删除所有满足 \(C_i \le T - L\) 的条目——也就是「从头开始累加，直到吃掉超出配额的那部分」。
4. 剩下的就是最近用到、合计约 \(L\) 字节的条目。

由于这种「累计窗口」按字节边界裁剪，删除量约为 \(T - L\)，保留量约为 \(L\)；边界处某个大 cubin 可能令保留量略高于 \(L\)，但总体收敛。

一条关键性质：当 \(T \le L\) 时，\(T - L \le 0\)，而 \(C_i\) 从第一个条目起就 \(>0\)，于是没有任何条目满足删除条件——**`evict_lru` 退化为无害的 no-op**。所以即便每次 store 后都调用，也只在真正超配额时才动手。

#### 4.4.3 源码精读

[src/cuda/tile/_cache.py:121-141](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cache.py#L121-L141) —— 这段 SQL 是全讲最烧脑的一句，逐层拆：

```sql
DELETE FROM cache WHERE key IN (
  SELECT key FROM (
    SELECT key,
           SUM(blob_size) OVER (ORDER BY atime, key) AS cumul_size   -- 前缀和
    FROM cache ORDER BY atime, key LIMIT ?                           -- 分批
  )
  WHERE cumul_size <= (SELECT SUM(blob_size) - ? FROM cache)         -- T - L
);
```

- 最内层用窗口函数 `SUM(...) OVER (ORDER BY atime, key)` 算出每行的累计字节数 `cumul_size`。
- 中层筛出 `cumul_size <= T - L` 的键——正是「最旧的、合计超出配额的那一段」。
- 最外层 `DELETE` 删掉它们。

外面的 Python `while` 循环处理「表很大、一次删不完」的情况：候选子查询带 `LIMIT row_limit`（初始 100），若本轮删够了一整批（`res.rowcount >= row_limit`，说明可能还有更多要删），就把 `row_limit` 放大 10 倍再删一轮；直到某轮删除数小于 `row_limit`，说明该删的都删完了，`break`。这是用「逐渐放大的批量」避免对超大表一次性做昂贵的全表窗口聚合。

调用点紧跟在 `cache_store` 之后：

[src/cuda/tile/_compile.py:562-564](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L562-L564) —— 每次新写入 cubin 后立刻按 `context.config.cache_size_limit` 淘汰一次。

#### 4.4.4 代码实践

1. **实践目标**：复现单元测试 `test_evict_lru` 的场景，亲手算出「哪几个条目会留下」，再用代码验证。
2. **操作步骤**：
   ```bash
   python -m pytest test/test_cache.py::test_evict_lru -v
   ```
   对照 [test/test_cache.py:82-109](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_cache.py#L82-L109) 理解：建 5 个条目各 1000 字节（共 5000），把它们的 `atime` 手动设成 0,1,2,3,4 使顺序确定，然后 `evict_lru(cache_dir, 3000)`。
3. **需要观察的现象**：调用前手动算：\(T=5000,\ L=3000,\ T-L=2000\)。按 `atime` 升序，前缀和依次是 1000、2000、3000、4000、5000。删除条件 \(C_i \le 2000\) 命中前两条（下标 0、1），所以下标 2、3、4 留下。
4. **预期结果**：断言 `remaining == keys[2:]`——最新的三个存活，与你手算一致。
5. 这是确定性逻辑（不依赖 GPU），结果可预期，无需「待本地验证」。

#### 4.4.5 小练习与答案

**练习**：把上面场景里的配额从 3000 改成 0，会删掉几个条目？为什么不是全删？

**答案**：会删掉前 4 个，留下下标 4 一个（1000 字节）。因为 \(T-L = 5000-0 = 5000\)，前缀和 \(C_i \le 5000\) 命中前 4 条（前缀和 1000/2000/3000/4000 都 ≤5000，第 5 条前缀和正好等于 5000 也 ≤5000，故也被删）——实际上 5 条的前缀和都 ≤5000，会全删。重新核对：第 5 条 \(C_5 = 5000 \le 5000\) 成立，所以 5 条全部删除。这道题的要点是：边界「等于」也会被删，所以配额设太小可能把缓存清空。

---

## 5. 综合实践

把四个模块串起来，完成本讲开头的实战任务。它分「无 GPU 也能做」与「有 GPU 端到端」两条路径，按你的环境二选一。

### 路径 A（推荐，无 GPU 也能做）：直接驱动缓存 API 模拟一次「命中」

```python
# 示例代码：无需 GPU / tileiras，纯逻辑验证
import os, tempfile
from cuda.tile._cache import cache_key, cache_store, cache_lookup, evict_lru

cache_dir = tempfile.mkdtemp()

# 模拟「第一次编译」：算键、未命中、写入
key = cache_key("v1.0", "sm_90", 3, b"<bytecode-of-vector_add>")
assert cache_lookup(cache_dir, key) is None          # 第一次：miss
cache_store(cache_dir, key, b"<cubin-bytes>")        # 编译完写入

# 模拟「第二次启动同一内核」
assert cache_lookup(cache_dir, key) == b"<cubin-bytes>"   # 第二次：hit！

# 模拟「改了 opt_level」→ 键变 → 又 miss
key2 = cache_key("v1.0", "sm_90", 2, b"<bytecode-of-vector_add>")
assert key2 != key
assert cache_lookup(cache_dir, key2) is None          # opt 变了：miss
```

- **目标**：亲眼看到「同输入命中、改 opt_level 漏命中」。
- **观察**：第一次 `lookup` 返回 `None`、第二次返回写入的字节；`key2 != key`。
- **预期**：上述断言全部成立。

### 路径 B（有 GPU 与 `tileiras`）：真实内核的缓存命中

1. **准备**：装好 `cuda-tile[tileiras]`（见 u1-l2），写一个最小 `vector_add` 内核（参考 u3-l1）。
2. **第一步·清空缓存**：
   ```bash
   export CUDA_TILE_CACHE_DIR=/tmp/cutile_cache_a
   rm -rf /tmp/cutile_cache_a
   ```
3. **第二步·首次启动**（编译并写入）：
   ```python
   ct.launch(stream, grid, kernel, args)   # 第一次：触发 tileiras，耗时较长
   ```
   结束后 `ls /tmp/cutile_cache_a` 应能看到 `cache.db`，`sqlite3 ... "SELECT count(*) FROM cache"` 应为 1。
4. **第三步·再次启动同一内核**：
   ```python
   ct.launch(stream, grid, kernel, args)   # 第二次：命中缓存，明显更快
   ```
   开 `CUDA_TILE_LOGS` 或对比两次耗时，第二次应显著快于第一次。
5. **第四步·改 opt_level 观察 key 变化**：用不同的 `CompilerOptions(opt_level=...)`（或 `ct.compiler_timeout` 风格的临时改写，参见 u8-l5）启动，会发现 `cache.db` 里多出一条记录——因为键变了。
6. **预期结果**：第二次命中缓存跳过 `tileiras`；改 opt_level 后产生新键、新条目。
7. 若无 GPU，路径 B 的耗时对比属「待本地验证」，请改做路径 A。

## 6. 本讲小结

- cuTile 用一个单文件 SQLite 库 `cache.db` 缓存**编译产物 cubin**（不是字节码），缓存命中即可跳过昂贵的 `tileiras` 子进程调用。
- 缓存键 `cache_key` = SHA-256(`_CACHE_VERSION ‖ compiler_version ‖ sm_arch ‖ opt_level|device_debug ‖ bytecode`)，覆盖所有会影响 cubin 的输入；其中 `opt_level/device_debug` 用的是**实际生效值**而非用户设定值。
- 表只有四列 `key/blob/blob_size/atime`；`cache_lookup` 命中时刷新 `atime`（LRU 续命），`cache_store` 用 `INSERT OR IGNORE` 幂等写入。
- `evict_lru` 是「按 `atime` 升序 + 前缀和累计窗口」淘汰：删除累计字节数 \(\le T-L\) 的最旧条目，保留约 \(L\) 字节的最近条目；\(T\le L\) 时自动退化为 no-op。
- 配置来自 `TileContextConfig`：`CUDA_TILE_CACHE_DIR`（默认 `~/.cache/cutile-python`，设 `off/0/none/""` 禁用）与 `CUDA_TILE_CACHE_SIZE`（默认 2 GiB），在 C++ 扩展构造 `default_tile_context` 时一次性从环境读取。
- 缓存全程「尽力而为」：所有错误（含数据库损坏）都被捕获、静默自愈或返回 `None`，绝不影响内核正确性。

## 7. 下一步学习建议

- 想看缓存在整条 `compile_tile` 流水线里的位置，回看 u5-l2（`compile_tile` 总流程）与本讲引用的 [src/cuda/tile/_compile.py:522-564](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L522-L564)。
- 想了解「为什么不同 tile 尺寸/静态形状会产生不同字节码、进而产生不同缓存键」，结合 u3-l5（`Constant` 嵌入）与 u3-l7（静态形状特化）。
- 后续 u8-l1 会讲 `launch` 的运行时调度与调用约定，u8-l5 会汇总所有调试/性能环境变量（含本讲的两个 `CUDA_TILE_CACHE_*`），可作为速查。
- 建议动手改 `test/test_cache.py`：把 `test_evict_lru` 的条目大小或配额改一改，先手算预期幸存者，再跑测试验证——这是巩固淘汰算法最快的路径。
