# u3-l8 JIT 缓存机制：避免重复编译

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 pyasc 的两级缓存结构：进程内的 `kernel_cache` 字典与跨进程落盘的 `FileCacheManager`，以及 `_cache_kernel` 中「查内存 → 查文件 → 都没有才编译」的判定顺序。
2. 逐项列出 `cache_factors` 的五要素（codegen 选项、compile 选项、constexpr 值、运行时参数类型、函数名），并能解释「为什么 ConstExpr 的**值**进缓存 key，而运行时参数只有**类型**进」。
3. 理解 `pyasc_key()` 如何把整个 Python 前端（codegen、language 全部模块）和 C++ 扩展 `libpyasc` 的哈希编进文件缓存 key，实现「升级 pyasc 后旧缓存整体失效」。
4. 掌握 `always_compile=True` 的精确语义：三道 `if not always_compile` 闸门分别跳过「读内存缓存、读文件缓存、写文件缓存」，因此强制重编运行**既不读也不污染**缓存。
5. 读懂 `FileCacheManager.put` 的原子写策略：每次写入先落到带 `pid + uuid` 的独立临时目录，再用 `os.replace` 原子改名，保证读者永远不会看到半个文件。

## 2. 前置知识

本讲建立在前几讲的基础上，先用两分钟把需要的背景串起来：

- **编译链路很贵（为什么需要缓存）**：一次完整的 JIT 编译要走「AST → ASC-IR → MLIR Pass → Ascend C 源码 → 毕昇编译器 → `.o` 二进制」（见 u1-l5）。其中最重的一步是调用外部毕昇编译器。如果每次调用 kernel 都重跑整条链路，写一个 8 次 tile 循环的算子就要编译 8 次。缓存的目标是：**同样的输入，只编译一次**。
- **缓存 key 的另一半来自 u3-l2**：`Function.cache_key` 属性在装饰时惰性计算一次，由「源码哈希 + 起始行号 + ConstExpr 全局依赖清单」哈希而成。本讲的文件缓存 key 会把它作为输入之一。
- **ConstExpr 与运行时参数的分流（u3-l3）**：`split_args` 按类型标注把实参切成 `runtime_args`（进设备侧 ABI）与 `constexprs`（编译期常量，直接编进生成的 Ascend C 代码）。
- **几个 Python 标准库工具**：
  - `pickle`：把 Python 对象序列化成字节流（这里用来把编译产物 `CompiledKernel` 落盘）。
  - `functools.lru_cache`：装饰一个纯函数，相同入参只算一次，后续直接返回记忆化结果。
  - `os.replace`：在 POSIX 系统上对文件做原子改名——要么成功，要么失败，不存在「改了一半」的中间状态。
  - `dataclass`：用类声明自动生成 `__init__` 等方法的语法糖，`vars(obj)` 可拿到其字段字典。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/runtime/cache.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py) | 缓存基础设施：`CacheOptions`（缓存目录配置）、`FileCacheManager`（落盘读写与原子写）、`pyasc_key`（前端指纹）、两个 key 哈希函数 |
| [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) | 缓存的调用方：`JITFunction.kernel_cache`（内存缓存字典）、`_gen_cache_factors`（拼五要素）、`_cache_kernel`（两级查找主流程） |
| [python/asc/codegen/function.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py) | `Function.cache_key` 属性：源码身份哈希，作为文件缓存 key 的输入 |
| [python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) | `CompileOptions` 数据类，其中 `always_compile` 字段是本讲的绕过开关 |
| [examples/02_add_framework/add_framework.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py) | 综合实践素材：带 `asc.ConstExpr[int]` 参数的 Add 算子 |

## 4. 核心概念与源码讲解

### 4.1 两级缓存总览与 `_cache_kernel` 主流程

#### 4.1.1 概念说明

pyasc 的缓存分两级，各自解决不同的「重复编译」问题：

- **第一级：内存缓存 `kernel_cache`**。它是 `JITFunction` 实例上的一个普通 Python 字典（构造时创建），key 是内存缓存 key，value 是编译产物 `CompiledKernel` 对象。它解决的是**同一进程内**反复调用同一个 kernel 的问题——比如一个推理循环里每个 batch 都调一次 `vadd_kernel`，只有第一次真正编译。进程退出即失效。
- **第二级：文件缓存 `FileCacheManager`**。它把 `CompiledKernel` 用 pickle 序列化后写到磁盘缓存目录，解决的是**跨进程**重复编译的问题——今天写好的脚本明天再跑，不必重新编译。磁盘上的缓存会一直留存，直到缓存 key 变化或目录被删。

两级的关系是「先查快的，再查慢的，都没有才编译，编译完两级都写」。

#### 4.1.2 核心流程

`_cache_kernel` 的完整判定流程（伪代码）：

```text
输入: runtime_args(运行时实参), constexprs(编译期常量),
      codegen_options, compile_options

1. arg_types     = 对每个 runtime_arg 调 get_arg_type 得到参数类型表
2. cache_factors = _gen_cache_factors(五要素拼接的字符串)
3. mem_key       = sha256(cache_factors)

4. 若 (不是 always_compile) 且 kernel_cache 里有 mem_key:
       命中内存缓存 → 直接返回 CompiledKernel        # 最快路径

5. file_key      = sha256(pyasc_key + 源码cache_key + cache_factors)
6. manager       = FileCacheManager(base32(file_key))
7. 若 (不是 always_compile) 且 磁盘上存在 <函数名>.o:
       命中文件缓存 → pickle.load 反序列化 → 返回     # 次快路径

8. 否则:
       mod    = _run_codegen(...)      # AST → ASC-IR
       kernel = _run_compiler(mod)     # Pass + Ascend C + 毕昇编译
       kernel_bin = pickle.dumps(kernel)

9. 若 (不是 always_compile) 且 第 7 步没命中文件缓存:
       manager.put(kernel_bin)         # 写文件缓存
       kernel_cache[mem_key] = kernel  # 写内存缓存

10. 返回 kernel
```

注意两个不对称的细节：

- 第 8 步的编译结果**总是**会填进内存缓存吗？不——第 9 步的写回也带 `always_compile` 守卫，强制重编时**两级都不写**。
- 命中文件缓存（第 7 步）时，只反序列化返回，**不会回填内存缓存**。第 9 步写回的条件是「不是 always_compile **且** `cached_kernel_file is None`」，文件命中时该条件为假，写文件与写 `kernel_cache` 被一起跳过。也就是说：内存缓存只在「实际发生编译」的分支被填充；若某个 key 在本进程首次就走文件命中路径，那么这个进程后续每次调用它都会重复一遍磁盘读取与 pickle 反序列化。这是当前实现的一个特征，值得在做性能实验时留意。

#### 4.1.3 源码精读

内存缓存字典在构造函数里创建，每个被 `@asc.jit` 修饰的函数各有一份私有缓存：[python/asc/runtime/jit.py:44-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L44-L46)（`self.default_options`、`self.launch_options`、`self.kernel_cache = {}`——最后这个就是第一级缓存）。

主流程 `_cache_kernel`：[python/asc/runtime/jit.py:156-182](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L156-L182)。逐段看：

- 第 157 行：`arg_types = {name: self.get_arg_type(value) ...}`——对运行时实参**只取类型不取值**（`get_arg_type` 的映射规则见 u3-l3）。
- 第 158-160 行：拼 `cache_factors` 并哈希出内存 key。
- 第 160-162 行：第一道闸门——内存命中且未要求强制重编，直接返回，这是最常见的高速路径。
- 第 164-167 行：组装文件缓存。`get_file_cache_key(self.cache_key, cache_factors)` 把**源码身份哈希**也编进来；`get_cache_manager` 创建管理器；缓存文件名就是「函数名 + `.o`」。
- 第 169-175 行：第二道闸门——文件命中则 `pickle.load`；否则走 `_run_codegen`（AST→IR）与 `_run_compiler`（Pass + 毕昇编译），并把结果 `pickle.dumps` 备用。
- 第 178-180 行：第三道闸门——只有「未强制重编且文件原本 miss」时才写文件缓存，同时（无论哪个分支编译出的 kernel）填内存缓存。

`_run` 对它的调用点：[python/asc/runtime/jit.py:204-212](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L204-L212)。`_run` 先合并选项、绑定签名、`split_args` 分流，然后 `_cache_kernel` 拿到二进制，`_run_launcher` 下发执行。**缓存只作用于「编译」，不作用于「下发」**——每次调用 kernel 都要重新打包参数、下发任务。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：在不运行任何程序的前提下，把 `_cache_kernel` 的四条退出路径背下来。
2. **操作步骤**：打开 [python/asc/runtime/jit.py:156-182](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L156-L182)，用三种颜色的笔标注：①内存命中返回（161-162 行）、②文件命中返回（169-172 行）、③编译后返回（173-176 行）；再单独标出第 178 行的写回守卫。
3. **需要观察的现象**：`if not compile_options.always_compile` 这个条件在 161、169、178 行出现了三次，且 178 行多了一个 `cached_kernel_file is None` 条件。
4. **预期结果**：能回答「同一个进程内，第 3 次以相同参数调用 kernel 会走哪条路径」（答：内存命中，161 行返回）。

#### 4.1.5 小练习与答案

**练习 1**：两个不同的 `@asc.jit` 函数（比如示例里的 `vadd_kernel` 和 `copy_in`）会共享 `kernel_cache` 字典吗？

答案：不会。`kernel_cache` 是实例属性（[jit.py:46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L46)），每个 JITFunction 各持有一份。加上 `fn_name` 也编在 cache_factors 里，两级 key 都天然区分不同函数。

**练习 2**：为什么缓存的对象是 `CompiledKernel` 而不是最终的 `.o` 二进制字节本身？

答案：`CompiledKernel` 除了二进制还携带 CoreType、调试开关、参数 ABI 表等元数据（见 u3-l5），Launcher 下发时需要这些信息。pickle 整个对象可以一次恢复全部上下文，不必再从外部补元数据。

### 4.2 `cache_factors` 五要素与 `get_mem_cache_key` / `get_file_cache_key`

#### 4.2.1 概念说明

缓存的核心问题是**「什么变了必须重编」**。pyasc 的回答是把所有可能影响生成代码的因素拼成一个字符串 `cache_factors`，再哈希成 key。它由五段组成，段内用 `;` 分隔、段间用 `__` 分隔：

| 段 | 内容 | 什么变化会触发 | 为什么必须进 key |
| --- | --- | --- | --- |
| 1 | `CodegenOptions` 全部字段 `名=值` | 例如 codegen 阶段选项变化 | 影响 AST→IR 的生成过程 |
| 2 | `CompileOptions` 全部字段 `名=值` | 例如 `debug`、`insert_sync`、`opt_level` 变化 | 影响 Pass 调度与毕昇编译命令（**含 `always_compile` 自身**） |
| 3 | `constexprs` 的 `名=repr(值)` | **ConstExpr 实参的值**变化 | 常量值被直接编进生成的 Ascend C 代码（缓冲字节数、循环上界等） |
| 4 | `arg_types` 的 `名=类型类名:类型名` | 运行时实参的**类型**变化（int 换成 tensor） | 类型决定 kernel 签名与参数 ABI |
| 5 | `fn_name=完整函数名` | 换一个 kernel 函数 | 区分不同函数 |

其中最值得咀嚼的是第 3、4 段的**不对称**：

- ConstExpr 参数是**编译期烘焙**的——`tile_length=128` 和 `tile_length=256` 会生成不同的 Ascend C 代码（UB 缓冲大小不同），所以**值**必须进 key。
- 运行时参数（如 `block_length`）只影响**下发时**塞进参数 blob 的数值（见 u3-l6），不影响生成的二进制，所以只有**类型**进 key。这就是「传 1024 还是 2048 个元素不会触发重编」的原理。

#### 4.2.2 核心流程

两个哈希函数的差别在于「身份信息」的多少：

```text
get_mem_cache_key(cache_factors)
    = sha256(cache_factors)                          # 只看调用因素

get_file_cache_key(fn_cache_key, cache_factors)
    = sha256( pyasc_key() ++ "__" ++ fn_cache_key ++ "__" ++ cache_factors )
    #  ↑ 工具链指纹        ↑ 源码身份哈希(u3-l2)        ↑ 调用因素
```

为什么内存 key 不含源码哈希？因为在**同一进程内**，源码在装饰时就被捕获并视为不可变（u3-l2 的结论），且 `kernel_cache` 是每个 JITFunction 实例私有的——不存在「进程跑着跑着源码变了」的场景。而文件缓存要**跨进程**存活：昨天的缓存目录里可能躺着旧版源码编出的 kernel，所以必须把源码身份（`fn_cache_key`）和 pyasc 版本（`pyasc_key()`）都编进去。

两个函数都挂了 `@functools.lru_cache()`：key 计算本身也被记忆化，同一组输入在一个进程里只哈希一次。

#### 4.2.3 源码精读

五要素拼接：[python/asc/runtime/jit.py:137-154](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L137-L154)。

- 140-141 行：第一段，`vars(codegen_options)` 展开 CodegenOptions 字段。
- 142-143 行：第二段，`vars(compile_options)` 展开 CompileOptions 字段——注意 `always_compile` 也在其中，这个细节在 4.5 节会发酵。
- 144-145 行：第三段，constexprs 用 `repr(val)`，**值**参与。
- 146-148 行：第四段，arg_types 的格式是 `参数名=参数类型类名:类型描述`，类型描述由 [python/asc/runtime/jit.py:113-123](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L113-L123) 的 `get_arg_dtype` 给出（Plain/Pointer 取 dtype 字符串，Struct/IR 取 py_type）。
- 149-150 行：第五段，函数名。
- 152-154 行：段间用 `__` 连接成最终字符串。

两个哈希函数：[python/asc/runtime/cache.py:139-150](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L139-L150)。`get_file_cache_key`（139-143 行）把 `pyasc_key()`、`fn_cache_key`、`cache_factors` 三者用 `__` 拼接后取 sha256；`get_mem_cache_key`（146-150 行）只对 `cache_factors` 取 sha256。两者都以 `@functools.lru_cache()` 装饰。

文件缓存 key 的输入之一 `self.cache_key` 来自 Function 基类：[python/asc/codegen/function.py:63-87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L63-L87)。它惰性计算一次：先用占位哈希打断递归（67-69 行），再用 `DependenciesFinder` 遍历源码拿到依赖哈希，拼上起始行号（80 行），再把引用到的 **ConstExpr 全局变量**的值追加进去（83-85 行），最后整体 sha256。回顾 u3-l2 的警告：普通全局变量改值不进任何 key——因为 83-85 行的过滤条件是 `isinstance(val, ConstExpr)`。

#### 4.2.4 代码实践

1. **实践目标**：直观看到「ConstExpr 值变 → key 变；普通参数值变 → key 不变」。
2. **操作步骤**（无需 NPU，只需能 `import asc`；若环境未安装，步骤结果**待本地验证**）：

```python
# key_probe.py（示例代码）
from asc.runtime.cache import get_mem_cache_key

f1 = "debug=False;opt_level=3__tile_length=128__x=PointerArgType:float32__fn_name=vadd_kernel"
f2 = f1.replace("tile_length=128", "tile_length=256")   # ConstExpr 值变化
f3 = f1.replace("x=PointerArgType:float32", "x=PointerArgType:float16")  # 参数类型变化

print(get_mem_cache_key(f1) == get_mem_cache_key(f1))  # True（lru_cache 也命中）
print(get_mem_cache_key(f1) == get_mem_cache_key(f2))  # False：ConstExpr 值变 → 新 key
print(get_mem_cache_key(f1) == get_mem_cache_key(f3))  # False：类型变 → 新 key
print(get_mem_cache_key.cache_info())                  # 观察 lru_cache 的命中统计
```

3. **需要观察的现象**：三行布尔输出依次为 `True / False / False`；`cache_info()` 显示 hits ≥ 1。
4. **预期结果**：验证了 key 对「ConstExpr 值」与「参数类型」敏感。注意脚本里的 `f1/f2/f3` 是手工模拟的 cache_factors 字符串，仅用于演示哈希函数的敏感性，真实的 factors 由 `_gen_cache_factors` 按固定顺序生成。

#### 4.2.5 小练习与答案

**练习 1**：把 `vadd_kernel` 的输入从 `torch.float32` 张量换成 `torch.float16` 张量，会重编吗？把张量的**内容**换掉（长度不变），会重编吗？

答案：前者会——第四段的 `PointerArgType:float32` 变成 `float16`，key 变化；后者不会——值不进 key，只影响下发时的数据。

**练习 2**：为什么 `cache_factors` 里还要显式放 `fn_name`？内存缓存不是每个函数一份字典吗？

答案：内存缓存确实按实例隔离，`fn_name` 对它是冗余的；但文件缓存 key 也复用同一份 `cache_factors`，而文件缓存目录按 key 全局共享，必须靠 `fn_name` 区分不同函数。一段 factors 同时服务两级 key，就统一带上了函数名。

### 4.3 `FileCacheManager`：目录布局与原子写

#### 4.3.1 概念说明

`FileCacheManager` 负责缓存的实际落盘，回答三个问题：

- **放哪里**：缓存根目录默认是 `~/.pyasc/cache`，可用环境变量 `PYASC_CACHE_DIR` 覆盖（`PYASC_HOME` 可改「家」的位置）。根目录下每个缓存 key 对应一个子目录，子目录名是 key 的 base32 编码，目录内就是 `<函数名>.o` 文件（pickle 流，不是裸 ELF）：

```text
$PYASC_CACHE_DIR（默认 ~/.pyasc/cache）
└── MFZ...(base32 编码的 key).../     # 一个 key 一个目录
    ├── vadd_kernel.o                  # pickle 序列化的 CompiledKernel
    └── lock                           # 锁文件路径（见下文）
```

- **怎么保证写一半不被读到**：经典的「临时目录 + 原子改名」两步写。先在缓存目录里建一个带 `pid + uuid` 的独有临时目录，把文件完整写进去，然后用 `os.replace` 把文件原子地搬到目标位置。POSIX 保证 `os.replace` 要么整体成功、要么失败，读者永远不会看到半截文件。
- **并发怎么办**：两个进程同时写同一个 key 时，各自写各自的临时目录（uuid 保证不撞名），最后只有一次 `os.replace` 生效——后写者赢，但两个版本都是完整合法的 kernel，所以无所谓。源码里还预留了 `lock_path`（`<缓存目录>/lock`）并在写入前校验它非空，但**当前版本并没有真正对锁文件加锁**（没有任何 flock/fcntl 调用）——并发安全实际完全依赖上面的「独有临时目录 + 原子改名」。这是仔细读源码才能发现的实现现状。

一个容易踩的坑：缓存目录配置在 `cache_options = CacheOptions()` 这个**模块级单例**里，而 dataclass 字段默认值（`os.getenv(...)`）在**导入 asc 时**求值一次。所以 `PYASC_CACHE_DIR` 必须在 `import asc` **之前**设置，进程内再改 `os.environ` 不会生效。

#### 4.3.2 核心流程

写入流程（`put`）：

```text
put(data, filename):
1. 组目标路径 filepath = 缓存目录/filename
2. 建独有临时目录  缓存目录/tmp.pid_{进程号}_{uuid}
3. 把 data 完整写入 临时目录/filename
4. os.replace(临时文件, filepath)    # 原子改名，读者看不到半截文件
5. os.removedirs(临时目录)           # 清理；顺带尝试删空的父目录（失败被忽略）
```

读取流程（`get_file`）：只判断文件是否存在，存在返回**路径字符串**（调用方自己去 open/pickle.load），不存在返回 `None`。

#### 4.3.3 源码精读

缓存目录配置：[python/asc/runtime/cache.py:20-26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L20-L26)。`CacheOptions` 是冻结 dataclass，`home_dir` 取 `PYASC_HOME`，`dir` 取 `PYASC_CACHE_DIR`、缺省落在 `~/.pyasc/cache`；第 26 行 `cache_options = CacheOptions()` 在模块导入时固化配置。

抽象基类：[python/asc/runtime/cache.py:29-37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L29-L37)。`CacheManager` 只约定 `get_file`/`put` 两个接口——当前唯一实现是 `FileCacheManager`，但接口留出了换缓存后端（例如远程缓存）的余地。

构造函数：[python/asc/runtime/cache.py:40-53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L40-L53)。接收 key，把缓存目录定为 `配置目录/key`，预留 `lock` 锁文件路径（50 行），并 `os.makedirs(exist_ok=True)` 建目录。

原子写主体：[python/asc/runtime/cache.py:66-92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L66-L92)。

- 77-82 行：`uuid` 防撞名、`pid` 便于排查是谁写的，临时目录名 `tmp.pid_{pid}_{rnd_id}`。
- 86-87 行：先写临时文件。
- 88-90 行：注释明说「`os.replace` 在 POSIX 上成功即原子」，随后改名。
- 91 行：`os.removedirs` 删掉空临时目录；它还会递归尝试删父目录，但缓存目录里已有别的缓存文件（非空）导致失败——该失败按 `os.removedirs` 的语义被忽略，不会误删缓存。

key 转目录名：[python/asc/runtime/cache.py:98-105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L98-L105)。`_base32` 把十六进制 key 转 base32 去掉填充符——hex 只用了字母表一半的信息密度，base32 编码后目录名更短；`get_cache_manager` 是唯一入口，永远用 base32 后的 key 构造管理器。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到缓存目录结构与「环境变量必须先于 import 设置」。
2. **操作步骤**（结果**待本地验证**，取决于是否已安装 pyasc）：

```bash
# 正确顺序：先设环境变量，再跑 Python
PYASC_CACHE_DIR=/tmp/pyasc_probe python3 -c "
from asc.runtime.cache import cache_options
print(cache_options.dir)          # 应打印 /tmp/pyasc_probe
"
# 反例：进程内再改环境变量无效
python3 -c "
import os
from asc.runtime.cache import cache_options
os.environ['PYASC_CACHE_DIR'] = '/tmp/other'
print(cache_options.dir)          # 仍打印默认 ~/.pyasc/cache
"
```

   跑过任意 pyasc 示例后，再执行 `ls $(python3 -c "from asc.runtime.cache import cache_options; print(cache_options.dir)")`，每个 base32 子目录里找一个 `.o` 文件。
3. **需要观察的现象**：第一条命令打印 `/tmp/pyasc_probe`；第二条仍打印默认目录；缓存目录下是若干 base32 命名的子目录，内含 `<函数名>.o`。
4. **预期结果**：确认配置在导入时固化、目录按 key 隔离。用 `python3 -m pickletools <某个>.o | head` 还能看到它确实是 pickle 流而非裸二进制（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么临时目录要同时带 `pid` 和 `uuid`？只用一个行不行？

答案：只用 `pid` 不行——同一进程多线程/多次写入会共用一个临时目录，前一次的半成品可能干扰后一次；只用 `uuid` 理论上够用（全局唯一），带上 `pid` 是为了运维排查：看到目录名就知道是哪个进程留下的垃圾。

**练习 2**：两个进程同时 miss 同一个 key、同时编译、同时 `put`，最终缓存里是什么？

答案：两次写各自走独立临时目录，两次 `os.replace` 依次生效，最终内容是**后改名者**的版本。但由于两次编译的输入（key）完全相同，产物语义等价，谁赢都正确——这正是无锁设计的立足点。

### 4.4 `pyasc_key`：整个前端的指纹

#### 4.4.1 概念说明

文件缓存可能存活很久（几天、几周），期间 pyasc 本身可能升级、前端源码可能被改（比如 `-e` 可编辑安装下直接改了 `python/asc/codegen` 里的文件，见 u1-l2）。旧前端编出的 kernel 对新前端来说不可信——IR 结构、代码生成逻辑都可能变了。`pyasc_key()` 就是解决这个问题的**工具链指纹**：它把「影响编译结果的整个软件栈」全部哈希一遍，任何一处变化都让文件缓存 key 整体改变、旧缓存全部失效。

它哈希三部分：

1. `cache.py` 自身（保证缓存逻辑自身变化也会失效）；
2. `asc.codegen.*` 与 `asc.language.*` 两个子包的**每一个模块文件**——用 `pkgutil.walk_packages` 遍历，逐文件 sha256；
3. C++ 扩展 `_C/libpyasc{EXT_SUFFIX}`（即 libpyasc.so）——按 1 MiB 分块流式哈希，避免大文件一次读入内存。

最后拼上前缀 `'0.0.0_'` 返回。前缀是版本号占位：将来 pyasc 发布正式版本，只需把占位换成版本串即可让全部历史缓存失效，不必等内容哈希变化。

`@functools.lru_cache()` 保证每个进程只算一次——遍历上百个模块文件并不便宜。

#### 4.4.2 核心流程

```text
pyasc_key():
1. h1 = sha256(cache.py 文件内容)
2. 对 codegen、language 两个目录 walk_packages:
       每个模块文件 → sha256 追加进列表
3. hN = 分块 sha256(_C/libpyasc{EXT_SUFFIX})
4. return '0.0.0_' + '_' 连接的所有哈希
```

它在 `get_file_cache_key` 里被拼进 key，所以**只影响文件缓存，不影响内存缓存**——内存缓存本来就活不过进程，工具链在进程内不可能变。

#### 4.4.3 源精读

[python/asc/runtime/cache.py:108-136](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L108-L136)。

- 111 行：`pyasc_path` 定位到 `python/asc` 目录（`cache.py` 的上两级）。
- 113-115 行：先哈希 `__file__`（即 cache.py 自己）。
- 117-124 行：两个 `(路径, 前缀)` 组合驱动 `pkgutil.walk_packages`，对找到的每个模块用 `lib.module_finder.find_spec(lib.name).origin` 拿到源文件路径并哈希。
- 127-135 行：后端部分。`sysconfig.get_config_var("EXT_SUFFIX")` 拿到当前平台扩展名（如 `.cpython-310-x86_64-linux-gnu.so`），按 1 MiB 块循环喂给 sha256。
- 136 行：`'0.0.0_' + '_'.join(contents)`——最终指纹是个长字符串，其中包含数十个 sha256。

在 `_cache_kernel` 里的消费点：[python/asc/runtime/jit.py:164](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L164) 调用 `get_file_cache_key`，后者第一段就拼 `pyasc_key()`（[cache.py:141](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L141)）。

#### 4.4.4 代码实践

1. **实践目标**：感受指纹的「大小」与记忆化。
2. **操作步骤**（**待本地验证**）：

```bash
python3 -c "
from asc.runtime.cache import pyasc_key
k = pyasc_key()
print('长度:', len(k))
print('前 48 个字符:', k[:48])
print(pyasc_key.cache_info())   # 两次调用，hits=1
pyasc_key()
print(pyasc_key.cache_info())   # hits=2，证明只算一次
"
```

3. **需要观察的现象**：key 很长（每个被哈希文件贡献 64 个十六进制字符 + 分隔符）；`cache_info()` 的 hits 随调用递增、miss 始终为 1。
4. **预期结果**：理解为什么该函数必须记忆化——每次 `_cache_kernel` 文件 miss 都要用它，重复遍历目录不可接受。

#### 4.4.5 小练习与答案

**练习 1**：用 `pip install -e .` 安装后，改了 `python/asc/language/basic/vec_binary.py` 里的一个字符串，重启进程再跑示例，会发生什么？

答案：`pyasc_key()` 变化 → `get_file_cache_key` 变化 → 文件缓存整体 miss → 重新编译。这正是可编辑安装下「改前端立即生效且不会用到旧缓存」的保障。

**练习 2**：`pyasc_key` 为什么把 C++ 扩展 `libpyasc` 也哈希进去？Python 前端不是已经全覆盖了吗？

答案：`libpyasc` 承载 MLIR Dialect 绑定、Pass 管理与 Ascend C 发射（u5 单元），同一份前端源码配上不同的后端扩展会产出不同的 Ascend C；只哈希 Python 侧无法发现后端升级。分块读取（1 MiB）则是因为 `.so` 可能有几十 MiB，流式哈希避免内存峰值。

### 4.5 `always_compile`：三道闸门的绕过语义

#### 4.5.1 概念说明

`always_compile` 是 `CompileOptions` 的一个布尔字段，默认 `False`。它专为调试设计：当你怀疑缓存里有脏数据、或想完整走一遍编译链路（比如配合 `PYASC_DUMP_PATH` 抓中间产物）时，传 `always_compile=True` 强制重编。

它的语义比「跳过缓存读取」更强，是**三道闸门**同时关上：

| 位置 | 代码 | 闸门效果 |
| --- | --- | --- |
| 闸门 1 | [jit.py:161](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L161) | 跳过**读内存缓存** |
| 闸门 2 | [jit.py:169](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L169) | 跳过**读文件缓存** |
| 闸门 3 | [jit.py:178](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L178) | 跳过**写文件缓存**（也不写内存缓存） |

还有一个隐蔽的**双重隔离**：`always_compile` 本身是 `CompileOptions` 的字段，于是它出现在 cache_factors 第二段里（`always_compile=True` 与 `always_compile=False` 拼出的字符串不同）。也就是说，即便没有这三道闸门，强制重编运行算出的 key 也和普通运行**不是同一个 key**——普通运行的缓存目录它根本找不到，自己的产物也不会覆盖普通缓存。闸门 + key 隔离，双保险确保「强制重编」对正常缓存体系**零污染**。

副作用也要知道：因为 key 变了，进程里交替使用 `always_compile=True/False` 调用同一 kernel，`kernel_cache` 中会出现两个条目（一个永远填不进去，一个正常）；且 `True` 那次每次调用都完整编译，不要用在生产路径。

#### 4.5.2 核心流程

```text
调用 vadd_kernel[8, stream](..., always_compile=True)
  → _run: extract_kwargs 把 always_compile 抽进 CompileOptions
  → _cache_kernel:
      闸门 1 关 → 不查 kernel_cache
      闸门 2 关 → 不查磁盘（且 file_key 本身就与普通运行不同）
      执行 _run_codegen + _run_compiler          # 完整编译
      闸门 3 关 → 不写磁盘、不写内存缓存
  → _run_launcher 正常下发执行
```

#### 4.5.3 源码精读

字段定义：[python/asc/runtime/compiler.py:27-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L27-L41)，`always_compile: bool = False` 在第 39 行，与 `debug`、`verify_sync`、`insert_sync` 等编译选项同袋。

三道闸门都在 `_cache_kernel` 里：[python/asc/runtime/jit.py:160-162](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L160-L162)（内存读）、[python/asc/runtime/jit.py:169-172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L169-L172)（文件读）、[python/asc/runtime/jit.py:178-180](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L178-L180)（文件写 + 内存写），全部以 `if not compile_options.always_compile and ...` 开头。

key 隔离的来源：[python/asc/runtime/jit.py:142-143](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L142-L143) 把 `vars(compile_options)` 整个拼进 cache_factors——`always_compile=True` 会以 `always_compile=True` 的字面量出现在第二段里。

传参路径：调用时写在小括号里即可，`_run` 的 `extract_kwargs` 会按 `CompileOptions` 的字段名把它从 kwargs 中抽走（[python/asc/runtime/jit.py:96-104](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L96-L104)、[python/asc/runtime/jit.py:206-207](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L206-L207)），所以它不会传给 kernel 函数本身。

#### 4.5.4 代码实践（源码阅读型）

1. **实践目标**：确认 `always_compile=True` 的运行「读不到也写不进」普通缓存。
2. **操作步骤**：先读 [jit.py:142-143](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L142-L143)，在纸上分别写出 `always_compile=False` 与 `True` 时第二段 factors 的样子；再对照三道闸门行号，推演「先正常跑一次、再带 `always_compile=True` 跑一次、最后再正常跑一次」三次调用各自的路径。
3. **需要观察的现象**：第三次正常调用应当命中第一次留下的缓存（不重编）。
4. **预期结果**：三次调用只有第二次真正编译；若第三次的耗时与第一次接近，说明你的推演有误，回到 161/169/178 三行检查。

#### 4.5.5 小练习与答案

**练习 1**：`@asc.jit(always_compile=True)` 写在装饰器里和写在中括号里分别合法吗？

答案：装饰器传参合法——装饰器 options 会成为 `default_options`，`_run` 第一步就与调用 kwargs 合并（[jit.py:205](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L205)），`always_compile` 属于 CompileOptions 关键字白名单（u3-l1）；但写在**中括号**里不行——`__getitem__` 只接受 `LaunchOptions` 的位置参数（核数、流），`always_compile` 不在其中。

**练习 2**：为什么设计上让强制重编「不写缓存」而不是「写回覆盖」？

答案：强制重编通常发生在调试场景（开了 `debug`、`print_ir_before_all` 等），此时编译产物可能带有调试注入，与正常选项下的产物不同源。若写回，会以调试产物占据一个 key；虽然因 key 隔离占的也不是普通 key，但「探测性运行不留下任何痕迹」是更稳妥的语义，避免任何路径下的缓存污染。

## 5. 综合实践：缓存命中三连测

设计一个贯穿本讲的实验：同一个 kernel，分别在「冷启动」「进程内复调」「跨进程复跑」「强制重编」「改 ConstExpr 值」五种情形下运行，观察耗时与缓存目录变化。素材取自 [examples/02_add_framework/add_framework.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py)。

### 5.1 准备探针脚本

新建 `cache_probe.py`（**示例代码**，kernel 部分逐行取自 02_add_framework，仅改写 Host 侧驱动加计时）：

```python
# cache_probe.py（示例代码）
import time
import torch
import asc
import asc.runtime.config as config
import asc.lib.runtime as rt
from asc.runtime.cache import cache_options

BUFFER_NUM = 2
USE_CORE_NUM = 8
TILE_NUM = 8


@asc.jit
def vadd_kernel(x: asc.GlobalAddress, y: asc.GlobalAddress, z: asc.GlobalAddress, block_length: int,
                tile_length: asc.ConstExpr[int]):
    offset = asc.get_block_idx() * block_length
    x_gm = asc.GlobalTensor()
    y_gm = asc.GlobalTensor()
    z_gm = asc.GlobalTensor()
    x_gm.set_global_buffer(x + offset)
    y_gm.set_global_buffer(y + offset)
    z_gm.set_global_buffer(z + offset)
    pipe = asc.TPipe()
    in_queue_x = asc.TQue(asc.TPosition.VECIN, BUFFER_NUM)
    in_queue_y = asc.TQue(asc.TPosition.VECIN, BUFFER_NUM)
    out_queue_z = asc.TQue(asc.TPosition.VECOUT, BUFFER_NUM)
    pipe.init_buffer(in_queue_x, BUFFER_NUM, tile_length * x.dtype.sizeof())
    pipe.init_buffer(in_queue_y, BUFFER_NUM, tile_length * y.dtype.sizeof())
    pipe.init_buffer(out_queue_z, BUFFER_NUM, tile_length * z.dtype.sizeof())
    for i in range(TILE_NUM * BUFFER_NUM):
        copy_in(i, x_gm, y_gm, in_queue_x, in_queue_y, tile_length)
        compute(z_gm, in_queue_x, in_queue_y, out_queue_z, tile_length)
        copy_out(i, z_gm, out_queue_z, tile_length)


@asc.jit
def copy_in(i: int, x_gm: asc.GlobalAddress, y_gm: asc.GlobalAddress, in_queue_x: asc.TQue,
            in_queue_y: asc.TQue, tile_length: asc.ConstExpr[int]):
    x_local = in_queue_x.alloc_tensor(x_gm.dtype)
    y_local = in_queue_y.alloc_tensor(y_gm.dtype)
    asc.data_copy(x_local, x_gm[i * tile_length:], tile_length)
    asc.data_copy(y_local, y_gm[i * tile_length:], tile_length)
    in_queue_x.enque(x_local)
    in_queue_y.enque(y_local)


@asc.jit
def compute(z_gm: asc.GlobalTensor, in_queue_x: asc.TQue, in_queue_y: asc.TQue, out_queue_z: asc.TQue,
            tile_length: asc.ConstExpr[int]):
    x_local = in_queue_x.deque(z_gm.dtype)
    y_local = in_queue_y.deque(z_gm.dtype)
    z_local = out_queue_z.alloc_tensor(z_gm.dtype)
    asc.add(z_local, x_local, y_local, tile_length)
    out_queue_z.enque(z_local)
    in_queue_x.free_tensor(x_local)
    in_queue_y.free_tensor(y_local)


@asc.jit
def copy_out(i: int, z_gm: asc.GlobalTensor, out_queue_z: asc.TQue, tile_length: asc.ConstExpr[int]):
    z_local = out_queue_z.deque(z_gm.dtype)
    asc.data_copy(z_gm[i * tile_length:], z_local, tile_length)
    out_queue_z.free_tensor(z_local)


def timed_launch(x, y, tag, **extra_options):
    z = torch.zeros_like(x)
    block_length = (z.numel() + USE_CORE_NUM - 1) // USE_CORE_NUM
    tile_length = block_length // TILE_NUM // BUFFER_NUM
    t0 = time.perf_counter()
    vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length, tile_length, **extra_options)
    dt = (time.perf_counter() - t0) * 1000
    print(f"[{tag}] 耗时 {dt:8.1f} ms | 结果正确: {torch.allclose(z, x + y)}")


if __name__ == "__main__":
    config.set_platform(config.Backend.Model, None)   # Model 仿真模式，无需 NPU
    print("缓存目录:", cache_options.dir)
    x = torch.rand(8 * 2048, dtype=torch.float32)
    y = torch.rand(8 * 2048, dtype=torch.float32)

    timed_launch(x, y, "第 1 次：冷启动，完整编译")
    timed_launch(x, y, "第 2 次：同进程，命中 kernel_cache")
    timed_launch(x, y, "第 3 次：always_compile=True 强制重编", always_compile=True)
```

### 5.2 执行与观察

```bash
# 环境变量必须在 import asc 之前生效，所以在命令行设置
rm -rf _cache_probe
PYASC_CACHE_DIR=$PWD/_cache_probe python3 cache_probe.py
echo "缓存目录数: $(ls _cache_probe | wc -l)"        # 预期：2（普通一次 + 强制重编一次）

# 跨进程复跑：第 1 次应远快于上一轮的冷启动（命中文件缓存）
PYASC_CACHE_DIR=$PWD/_cache_probe python3 cache_probe.py

# 改 ConstExpr 值：把脚本里 TILE_NUM = 8 改成 4，再跑
PYASC_CACHE_DIR=$PWD/_cache_probe python3 cache_probe.py
echo "缓存目录数: $(ls _cache_probe | wc -l)"        # 预期：新增 1 个（新 TILE_NUM 的普通 key）
```

### 5.3 预期现象与解释

| 观察 | 预期（待本地验证） | 对应源码 |
| --- | --- | --- |
| 同进程第 2 次调用 | 耗时骤降（毫秒级，只剩下发） | [jit.py:160-162](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L160-L162) 内存命中 |
| 新进程第 1 次调用 | 明显快于冷启动但仍高于内存命中（pickle 反序列化） | [jit.py:169-172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L169-L172) 文件命中 |
| `always_compile=True` 那次 | 耗时与冷启动同级；不新增普通缓存目录 | 三道闸门 + key 双重隔离 |
| `TILE_NUM` 改 4 后 | 首次调用重新变慢；缓存目录 +1 | `tile_length` 值变 → factors 第三段 `tile_length=64` → 新 key |

**为什么改 ConstExpr 值必然生成新缓存 key**：`TILE_NUM` 从 8 改成 4 使 Host 侧算出的 `tile_length` 变化，而 `tile_length` 被标注为 `asc.ConstExpr[int]`（[examples/02_add_framework/add_framework.py:29-30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L29-L30)），它作为**编译期常量**被烘进生成的 Ascend C 代码——`pipe.init_buffer(..., tile_length * x.dtype.sizeof())` 的 UB 缓冲字节数随之改变。所以不仅 key 要变（factors 第三段用 `repr` 记下了值），**必须**重编：不同 `tile_length` 对应的本来就是不同的二进制。这与运行时参数 `block_length` 形成对照——它只影响下发时参数 blob 里的数值，类型不变则二进制不变，key 也不变。

若观察到与上表不符的现象（例如第 2 次调用仍然很慢），优先检查：是否忘了在命令行设置 `PYASC_CACHE_DIR`；是否 `TILE_NUM` 改动后 `block_length // TILE_NUM // BUFFER_NUM` 不再整除导致走了异常路径。

## 6. 本讲小结

- pyasc 缓存分两级：`JITFunction.kernel_cache` 字典管进程内复调，`FileCacheManager` 管跨进程复用；`_cache_kernel` 按「查内存 → 查文件 → 编译 → 写回」推进，`CompiledKernel` 以 pickle 形式落盘为 `<函数名>.o`。
- 文件缓存 key = sha256(`pyasc_key` + 源码 `cache_key` + `cache_factors`)，内存缓存 key = sha256(`cache_factors`)——进程内源码视为不可变，所以内存 key 不含源码与工具链身份。
- `cache_factors` 五要素：CodegenOptions、CompileOptions、**ConstExpr 的值**、运行时参数的**类型**、函数名；「值进 key 与否」取决于该值是否被烘进生成的代码。
- `pyasc_key()` 把 cache.py 自身、codegen/language 两个子包的全部模块、`libpyasc{EXT_SUFFIX}` 逐文件哈希成工具链指纹，前端或后端任何变化都令文件缓存整体失效。
- `FileCacheManager.put` 用「pid+uuid 独有临时目录 + `os.replace` 原子改名」保证读者永不见半截文件；`lock` 路径已预留但当前版本未真正加锁，并发安全完全依赖原子写。
- `always_compile=True` 通过三道 `if not always_compile` 闸门同时关掉「读内存、读文件、写文件」，且该字段本身参与 cache_factors 形成 key 双重隔离——强制重编对正常缓存零污染。

## 7. 下一步学习建议

本讲补完了 runtime 模块的最后一块拼图。至此 u3 单元的 `jit.py`、`compiler.py`、`launcher.py`、`cache.py` 主链路已经走读完。建议接下来：

1. **进入 u4 单元（codegen 模块）**：缓存 miss 后的第一步 `_run_codegen` 会创建 `FunctionVisitor` 遍历 AST（[python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py)），从 u4-l1 的总览讲开始读。
2. **横向对照缓存设计**：把本讲的 `get_file_cache_key` 与 u3-l2 的 `Function.cache_key` 放在一起画一张「key 构成层次图」（工具链层 → 源码层 → 调用层），检验自己能否独立复述每一层的输入。
3. **延伸阅读**：u3-l7 提到 `build_npu_ext` 的产物也以 sha256 为 key 存文件缓存（`asc/lib/runtime` 的 build_utils 机制），可对照本讲体会「同一套哈希思想在不同子系统中的复用」。
