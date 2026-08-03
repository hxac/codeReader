# 测试基础设施：lit 与 lit.cfg

> 本讲永久链接基准（下文所有源码链接均基于此 HEAD）：
> `https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/`
>
> 链接格式约定：`[文件路径:起始行-结束行](<基准><相对路径>#L起始-L结束)`。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 compiler-rt 的测试是如何被 **lit** 组织、发现与执行的。
2. 看懂「公共配置 `lit.common.cfg.py` / `lit.common.configured.in`」与「子套件配置（如 `builtins/lit.cfg.py`）」之间的分工，理解配置加载的两层结构。
3. 独立运行一个测试套件（如 `check-builtins` / `check-compiler-rt`），并能用 `llvm-lit` 单独跑某个测试文件。
4. 正确使用 `REQUIRES:` / `XFAIL:` 与 `target-is-${arch}` 特性串，避开文档里反复强调的「目标三元组陷阱」。

本讲是 [u1-l3 构建系统入门](u1-l3-build-system.md) 的后续：那里我们配出了构建目录，这里我们让那个构建目录「跑起来」。

---

## 2. 前置知识

在进入源码前，先用大白话把三个概念讲清楚。

### 2.1 什么是 lit

**lit**（LLVM Integrated Tester）是 LLVM 生态的测试执行器。它的核心思想很简单：

- 把一个目录当成一个「测试套件（test suite）」。
- 套件里每一个匹配后缀（如 `.c`、`.cpp`）的文件就是一个测试。
- 测试文件自身的注释里写着 `// RUN: ...` 这样的命令行，lit 负责替换其中的占位符（substitution）并执行这些命令，根据命令的退出码判断通过/失败。

也就是说，compiler-rt 的测试**不是**用某种测试框架写出来的函数，而是一个个「自带运行命令的小程序」。lit 只是个搬运工兼裁判。

### 2.2 什么是 ShTest（shell test）格式

compiler-rt 用的是 lit 的 `ShTest` 测试格式。一句话：**每个测试 = 一段 shell 脚本 + 一组断言**。RUN 行里的命令在真实 shell 里跑，靠退出码（`&&`、`||`）和 `not`、`FileCheck` 这类工具来判定。

> 关键源码：`config.test_format = lit.formats.ShTest()`，见 [test/lit.common.cfg.py:113-114](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L113-L114) —— 这一行决定了 compiler-rt 全部测试都是「命令行式」的。

### 2.3 什么是配置的两层结构

lit 的配置分两层，这是本讲最重要的心智模型：

| 层级 | 文件 | 何时生成 | 作用 |
|---|---|---|---|
| 第一层：公共配置 | `test/lit.common.cfg.py`（源码）+ `test/lit.common.configured.in`（CMake 模板） | 配置（CMake）阶段由模板生成 `lit.common.configured` | 所有 sanitizer / builtins 套件**共享**的规则：替换符、特性（features）、环境变量、资源目录定位 |
| 第二层：子套件配置 | `test/<套件>/lit.cfg.py`（如 `builtins/lit.cfg.py`） | 源码，运行时加载 | 套件**特有**的规则：本套件的后缀、本套件专用的替换符（如 `%clang_builtins`） |

二者通过 `lit.site.cfg.py.in` 这个「胶水」模板串起来。下面 §4 会逐层拆解。

> 术语速查
> - **substitution（替换符）**：形如 `%clang`、`%run`、`%librt`，lit 在执行 RUN 行前会把它们替换成真实命令字符串。
> - **feature（特性）**：写入 `config.available_features` 的字符串，测试用 `REQUIRES:` / `XFAIL:` / `UNSUPPORTED:` 引用，决定测试是否在该配置下运行。
> - **target triple（目标三元组）**：形如 `x86_64-linux-gnu`，描述「为谁编译」。compiler-rt 经常一次构建同时测多个架构，这是陷阱的根源。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/TestingGuide.md](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/docs/TestingGuide.md) | 官方测试指南，明确讲了 `REQUIRES`/`XFAIL` 的目标三元组陷阱与 `target-is-${arch}` 解法 |
| [test/lit.common.configured.in](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.configured.in) | CMake 模板，把构建期变量（编译器路径、架构、libdir 等）填进 `config` 的属性 |
| [test/lit.common.cfg.py](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py) | 公共规则的真正实现：替换符、特性、资源目录定位、环境变量清理 |
| [test/builtins/lit.site.cfg.py.in](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/lit.site.cfg.py.in) | builtins 套件的胶水模板：先加载公共配置，再加载套件配置 |
| [test/builtins/lit.cfg.py](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/lit.cfg.py) | builtins 套件特有规则（后缀、`%clang`、`%macos_version_*`） |
| [test/builtins/Unit/lit.cfg.py](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/lit.cfg.py) | builtins 单元测试的规则（`%clang_builtins`、`%librt`、`librt_has_*` 特性） |
| [test/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/CMakeLists.txt) | 把各套件接成 `check-compiler-rt` / `check-<套件>` 目标 |
| [test/builtins/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/CMakeLists.txt) | 生成 builtins 的 lit 站点配置，并按实际编译进库的函数生成 `librt_has_*` 特性 |
| [test/builtins/Unit/divdi3_test.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/divdi3_test.c) | 一个真实的 builtins 测试样例（RUN + REQUIRES） |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 lit 公共配置规则**（`lit.common.configured.in` + `lit.common.cfg.py`）
2. **4.2 各子套件 lit.cfg.py**（以 builtins 为例）
3. **4.3 目标三元组与 REQUIRES/XFAIL**

### 4.1 lit 公共配置规则

#### 4.1.1 概念说明

compiler-rt 有几十个测试套件（asan、msan、tsan、builtins、profile、fuzzer、xray、orc……）。如果每个套件都各自写一遍「怎么找到 clang、怎么设置 `LD_LIBRARY_PATH`、怎么清理危险环境变量、怎么定位资源目录」，那将是灾难性的重复。

于是有了**公共配置层**：把所有套件都要做的事集中到 `test/lit.common.cfg.py`。但它需要知道一些**只能在构建期确定**的信息（编译器在哪、目标三元组是什么、库装在哪），这些值不能写死在源码里。解决办法是：用 CMake 模板 `test/lit.common.configured.in` 在配置阶段把这些值「填空」，生成一个 `lit.common.configured` 文件，再由它去加载 `lit.common.cfg.py`。

这就是「模板（填构建期变量）→ 加载实现（写公共逻辑）」的两步。

#### 4.1.2 核心流程

公共配置初始化的伪代码流程：

```
CMake 配置阶段：
  test/lit.common.configured.in
    --[填入 @COMPILER_RT_RESOLVED_TEST_COMPILER@ 等变量]-->
  <build>/test/lit.common.configured
        │
        │  它做了两件事（见 4.1.3 源码）：
        ▼
  1. set_default(): 给 config 写入一整套属性（clang、target_triple、libdir…）
  2. lit.llvm.initialize() + lit_config.load_config(lit.common.cfg.py)
        │
        ▼
  lit.common.cfg.py 执行公共逻辑：
     - 设 test_format = ShTest
     - 按 compiler_id 设 cxx/debug 标志、加 compiler_id 特性
     - 定位/覆盖 clang 资源目录（关键！见 4.1.4）
     - 清理 ASAN_OPTIONS 等危险环境变量
     - 注册 %clang / %run / %env / %expect_crash 等公共替换符
     - 注册 target_os / target_arch 等公共特性
```

要点：**构建期变量由模板注入，运行期逻辑由 `.cfg.py` 实现**。子套件无需重复这些。

#### 4.1.3 源码精读

**(a) 模板把构建期变量写进 config 属性**

模板用一个 `set_default` 辅助函数批量设置属性——「只有当属性未被预先设置时才填默认值」，这样命令行 `--param` 还能覆盖：

[test/lit.common.configured.in:4-6](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.configured.in#L4-L6)

```python
# Set attribute value if it is unset.
def set_default(attr, value):
  if getattr(config, attr, "") == "":
    setattr(config, attr, value)
```

紧接着是一长串 `set_default`，把测试编译器、目标三元组、库输出目录等填进去。挑几条关键的：

[test/lit.common.configured.in:9-32](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.configured.in#L9-L32)（节选）

```python
set_default("target_triple", "@COMPILER_RT_DEFAULT_TARGET_TRIPLE@")
set_default("target_cflags", "@COMPILER_RT_TEST_COMPILER_CFLAGS@")
set_default("target_arch", "@COMPILER_RT_DEFAULT_TARGET_ARCH@")
set_default("target_os", "@HOST_OS@")
...
set_default("clang", "@COMPILER_RT_RESOLVED_TEST_COMPILER@")
set_default("compiler_id", "@COMPILER_RT_TEST_COMPILER_ID@")
...
set_default("compiler_rt_libdir", "@COMPILER_RT_RESOLVED_LIBRARY_OUTPUT_DIR@")
```

中文说明：`@...@` 是 CMake 占位符，配置阶段被替换成真实路径。`config.clang` 就是「测试用的那个 clang 可执行文件」，`config.compiler_rt_libdir` 是「刚刚构建出来的 compiler-rt 库所在目录」——这两个值决定了「测的是哪个 clang」和「链接的是不是刚编出来的库」。

模板最后一行把控制权交给公共规则的实现：

[test/lit.common.configured.in:97-101](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.configured.in#L97-L101)

```python
import lit.llvm
lit.llvm.initialize(lit_config, config)
# Setup attributes common for all compiler-rt projects.
lit_config.load_config(config, "@COMPILER_RT_SOURCE_DIR@/test/lit.common.cfg.py")
```

中文说明：`lit.llvm.initialize` 接入 LLVM 的 lit 工具（如 `%python`、文件替换等）；`load_config` 加载并执行 `lit.common.cfg.py`——这才是公共逻辑的真身。

**(b) 公共规则：测试格式与编译器探测**

进入 `lit.common.cfg.py`，第一件实质的事是设定测试格式：

[test/lit.common.cfg.py:113-114](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L113-L114)

```python
# Setup test format.
config.test_format = lit.formats.ShTest()
```

中文说明：全项目都用 `ShTest`——测试通过执行 RUN 行的 shell 命令、看退出码来判定。

接着根据「测试编译器是 Clang / MSVC / GNU」分别设置 C++ 模式标志和调试信息标志，并把编译器 ID 本身注册成一个特性（供 `REQUIRES: Clang` 之类使用）：

[test/lit.common.cfg.py:119-143](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L119-L143)（节选）

```python
compiler_id = getattr(config, "compiler_id", None)
if compiler_id == "Clang":
    ...
    config.cxx_mode_flags = ["--driver-mode=g++"]
    config.debug_info_flags = ["-gline-tables-only"]
elif compiler_id == "MSVC":
    ...
else:
    lit_config.fatal("Unsupported compiler id: %r" % compiler_id)
# Add compiler ID to the list of available features.
config.available_features.add(compiler_id)
```

中文说明：sanitizer 报告需要「一点点调试信息」就能给出可读栈，所以默认用最轻量的 `-gline-tables-only`；最后把 `Clang`/`GNU`/`MSVC` 加进 `available_features`，测试可以据此要求特定编译器。

**(c) 定位 clang 的「资源目录」（resource dir）**——这是本模块最值得读的一段。

`find_compiler_libdir()` 询问 clang 自己「你的运行时库目录在哪」：

[test/lit.common.cfg.py:26-65](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L26-L65)（节选）

```python
def find_compiler_libdir():
    """Returns the path to library resource directory used by the compiler."""
    if config.compiler_id != "Clang":
        ...  # 非 Clang 不支持，返回 None
    # 优先用新版 clang 的 -print-runtime-dir
    runtime_dir, clang_cmd = get_path_from_clang(
        shlex.split(config.target_cflags) + ["-print-runtime-dir"], allow_failure=True)
    if runtime_dir:
        if os.path.exists(runtime_dir):
            return os.path.realpath(runtime_dir)
        ...
    # 回退：老版 AppleClang 用 -print-file-name=lib 再拼 darwin
    if config.target_os == "Darwin":
        lib_dir, _ = get_path_from_clang(["-print-file-name=lib"], allow_failure=False)
        runtime_dir = os.path.join(lib_dir, "darwin")
        ...
```

中文说明：资源目录定位有**优先级**——先试 `-print-runtime-dir`（新 clang 才有），失败再回退到 `-print-file-name=lib` 拼 `darwin`（针对老 AppleClang）。`get_path_from_clang`（[test/lit.common.cfg.py:17-24](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L17-L24)）就是「带着 `--target` 跑一次 clang、抓它的输出」。

拿到资源目录后，公共配置还会**判断要不要覆盖它**——如果用「已安装的 clang」去测「刚构建出来的 compiler-rt」，两者的资源目录不一致，就要强制让测试链接刚构建出来的库：

[test/lit.common.cfg.py:176-216](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L176-L216)（节选）

```python
test_cc_resource_dir, _ = get_path_from_clang(
    shlex.split(config.target_cflags) + ["-print-resource-dir"], allow_failure=True)
...
if (test_cc_resource_dir != local_build_resource_dir
        and config.test_standalone_build_libs):
    if config.compiler_id == "Clang":
        ...
        config.target_cflags += f" -resource-dir={config.compiler_rt_output_dir}"
        if not target_is_windows:
            config.target_cflags += f" -Wl,-rpath,{config.compiler_rt_libdir}"
```

中文说明：`-print-resource-dir` 问 clang「你默认的资源目录」，`config.compiler_rt_output_dir` 是「刚构建的 compiler-rt 输出目录」。两者不一致 + 要求测独立构建的库（`test_standalone_build_libs`）时，就用 `-resource-dir=...` 和 `-Wl,-rpath,...` 强行把刚构建的库塞给被测程序。**这段直接回答了实践任务里「clang 资源目录是如何被定位的」。**

**(d) 清理危险环境变量 + 注册公共替换符**

公共配置会**删掉**一批可能污染测试的环境变量（避免你本机的 `ASAN_OPTIONS` 干扰测试结果）：

[test/lit.common.cfg.py:259-290](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L259-L290)（节选）

```python
possibly_dangerous_env_vars = [
    "ASAN_OPTIONS", "DFSAN_OPTIONS", "HWASAN_OPTIONS", "LSAN_OPTIONS",
    "MSAN_OPTIONS", "UBSAN_OPTIONS", ...
]
for name in possibly_dangerous_env_vars:
    if name in config.environment:
        del config.environment[name]
```

中文说明：sanitizer 的运行时通过环境变量（如 `ASAN_OPTIONS`）配置行为，测试套件必须清空它们，否则你本机随手设的选项会让测试结果不可复现。

它还注册了一系列公共替换符，这里看三个最常用的来源：

- **`%run`**：在普通 Linux/macOS 主机上，它就是空串（直接跑）；在模拟器/iOS/Android 上才会被替换成「上传到设备再执行」的包装脚本。

  [test/lit.common.cfg.py:492-498](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L492-L498)

  ```python
  else:
      config.substitutions.append(("%run", ""))
      config.substitutions.append(("%env ", "env "))
      ...
  ```

  中文说明：所以 `// RUN: %run %t` 在本机等价于直接执行 `%t`；写成 `%run` 而非裸跑，是为了让同一个测试在「本机」「模拟器」「真机」三种环境都能用同一行命令。

- **`%clang` 禁用裸写**：公共配置故意把 `" clang"`（注意前面有空格）替换成一段报错文本，**禁止**在 RUN 行里直接写 `clang`，强制你用 `%clang`：

  [test/lit.common.cfg.py:327-335](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L327-L335)

  ```python
  config.substitutions.append(
      (
          " clang",
          """\n\n*** Do not use 'clangXXX' in tests,
               instead define '%clangXXX' substitution in lit config. ***\n\n""",
      )
  )
  ```

  中文说明：这样 `%clang` 才能统一带上正确的 `--target`、调试标志、资源目录等。`%clang` 这个具体替换符由**子套件** `lit.cfg.py` 定义（见 §4.2）。

- **`%expect_crash`**：用于「预期程序崩溃」的测试（例如测 ASan 检出错误后退出）：

  [test/lit.common.cfg.py:506-515](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L506-L515)

  ```python
  if os.name == "nt":
      config.expect_crash = "not KillTheDoctor "
  else:
      config.expect_crash = "not --crash "
  config.substitutions.append(("%expect_crash ", config.expect_crash))
  ```

  中文说明：`not --crash <程序>` 表示「这个程序应当崩溃；如果它正常退出则视为测试失败」。`not` 是 LLVM 自带的「反转退出码」工具。

**(e) 注册公共特性（features）**

公共配置还会把目标操作系统、架构等注册成特性，供 `REQUIRES`/`XFAIL` 使用：

[test/lit.common.cfg.py:305-311](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L305-L311)

```python
config.available_features.add(config.target_os.lower())
...
if re.match(r"^x86_64.*-linux", config.target_triple):
    config.available_features.add("x86_64-linux")
```

[test/lit.common.cfg.py:517-522](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L517-L522)

```python
target_arch = getattr(config, "target_arch", None)
if target_arch:
    config.available_features.add(target_arch + "-target-arch")
    if target_arch in ["x86_64", "i386"]:
        config.available_features.add("x86-target-arch")
    config.available_features.add(target_arch + "-" + config.target_os.lower())
```

中文说明：于是测试里可以写 `REQUIRES: x86_64-linux`、`REQUIRES: darwin` 之类。但请注意：**最推荐的「按架构」特性是 `target-is-${arch}`，它不在 `.cfg.py` 里，而在模板里**——见 §4.3。

#### 4.1.4 代码实践

**实践目标**：亲手验证「lit 是如何定位 clang 资源目录的」，并理解 `-print-resource-dir` 与 `-print-runtime-dir` 的区别。

**操作步骤**：

1. 在你已经配好的构建目录里（参考 [u1-l3](u1-l3-build-system.md)），找到被测的 clang（即模板里 `@COMPILER_RT_RESOLVED_TEST_COMPILER@` 指向的那个）。
2. 直接问它资源目录：
   ```bash
   <clang> --target=<你的三元组> -print-resource-dir
   ```
3. 再问它运行时目录（如果 clang 较新）：
   ```bash
   <clang> --target=<你的三元组> -print-runtime-dir
   ```
4. 把这两个输出，对照 [test/lit.common.cfg.py:181-183](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L181-L183) 和 [test/lit.common.cfg.py:40-42](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L40-L42) 看：lit 在配置阶段正是这样调用 clang 拿到这两条路径的。

**需要观察的现象**：

- `-print-resource-dir` 返回的通常是 `<clang>/lib/clang/<版本号>`，里面带 `include/`（内置头，如 `stdint.h`）。
- `-print-runtime-dir` 返回的目录里应能看到 `libclang_rt.*.a`（compiler-rt 的库）。

**预期结果**：你能用一句话说清——「lit 通过调用 `clang -print-resource-dir` / `-print-runtime-dir` 来定位资源目录；当它和刚构建的 compiler-rt 输出目录不一致、且要求测独立构建的库时，lit 用 `-resource-dir=` 与 `-Wl,-rpath=` 强制覆盖」。

> 若你的环境里 clang 不支持 `-print-runtime-dir`（输出为空），这正是源码里「回退分支」处理的情况，符合预期。
>
> 如果你尚未构建 compiler-rt、无法拿到 `<clang>` 路径，可先跳过命令部分，仅做源码阅读——结论一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `lit.common.configured.in` 用 `set_default(attr, value)`（先判断是否已设置）而不是直接 `setattr`？

**参考答案**：为了让命令行/上层配置能覆盖默认值。lit 支持 `--param <name>=<val>` 在运行时传参，`set_default` 保证只有当属性未被预先设定时才填模板默认值，从而给手动覆盖留出优先级。

**练习 2**：公共配置为什么要删除 `ASAN_OPTIONS` 等环境变量？

**参考答案**：这些环境变量会改变 sanitizer 运行时的行为（如 `halt_on_error`、`detect_leaks`）。测试用例的期望结果是固定的，开发者本机随手设的环境变量会让测试不可复现甚至误判，所以必须在测试环境里清空它们。

---

### 4.2 各子套件 lit.cfg.py（以 builtins 为例）

#### 4.2.1 概念说明

公共配置解决了「大家都要做的事」。但每个套件还有自己的特殊性：

- builtins 测试要链接静态库 `libclang_rt.builtins-<arch>.a`，于是需要 `%librt` 替换符。
- asan/msan 测试要加 `-fsanitize=address`，于是需要自己的 `%clang_xx` 变体。
- 不同套件关心的文件后缀不同（builtins 关心 `.c`；某些套件还关心 `.cpp`、`.m`）。

这些「套件特有」的事就放进 `test/<套件>/lit.cfg.py`。它运行在公共配置**之后**，可以读取公共配置已经设好的 `config.clang`、`config.target_cflags` 等属性，再叠加自己的规则。

#### 4.2.2 核心流程

一个套件的 lit 配置加载链（以 builtins 为例）：

```
lit 启动 → 读取 <build>/test/builtins/lit.site.cfg.py
            （由 lit.site.cfg.py.in 生成）
            │
            ▼
   lit.site.cfg.py.in 做两件事：
   1. load_config(lit.common.configured)   ← 第一层公共配置（含模板填的变量）
   2. load_config(builtins/lit.cfg.py)      ← 第二层套件配置
            │
            ▼
   builtins/lit.cfg.py：
     - 设 config.name = "Builtins"
     - 设 config.suffixes（决定哪些文件被当作测试）
     - 注册 %clang（带上 -Wall 等额外标志）
     - （Darwin）注册 %macos_version_*
```

关键：**胶水模板只负责「按顺序加载两层」，本身不含业务逻辑**。

#### 4.2.3 源码精读

**(a) 胶水模板：先公共，后套件**

[test/builtins/lit.site.cfg.py.in:4-7](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/lit.site.cfg.py.in#L4-L7)

```python
# Load common config for all compiler-rt lit tests.
lit_config.load_config(config, "@COMPILER_RT_BINARY_DIR@/test/lit.common.configured")
# Load tool-specific config that would do the real work.
lit_config.load_config(config, "@BUILTINS_LIT_SOURCE_DIR@/lit.cfg.py")
```

中文说明：第一行加载公共配置（注意它指向的是**构建目录**里那个被 CMake 填好变量的 `lit.common.configured`，不是源码模板）；第二行加载 builtins 自己的 `lit.cfg.py`。这个「双 load」模式是所有 compiler-rt 套件共用的。

**(b) builtins 套件配置：极简**

builtins 的功能测试套件 `lit.cfg.py` 非常短：

[test/builtins/lit.cfg.py:5-33](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/lit.cfg.py#L5-L33)

```python
config.name = "Builtins"
config.test_source_root = os.path.dirname(__file__)

# Test suffixes.
config.suffixes = [".c", ".cpp", ".m", ".mm", ".test"]
extra_flags = ["-Wall"]
if config.compiler_id == "GNU":
    extra_flags.append("-Werror=builtin-declaration-mismatch")

# Define %clang and %clangxx substitutions to use in test RUN lines.
config.substitutions.append(
    ("%clang ", " " + config.clang + " " + " ".join(extra_flags) + " ")
)
```

中文说明：

- `config.name` 是套件名，会显示在测试输出里。
- `config.suffixes` 决定 lit 把哪些后缀的文件当测试——这是「lit 如何发现测试」的开关。
- `config.test_source_root` 告诉 lit「从这个目录往下递归找测试」。
- `extra_flags`：用 GNU 编译器时，把「内建函数声明不匹配」升级为错误，避免错误声明被放过。
- **这里才真正定义了 `%clang` 替换符**：它 = `config.clang`（公共配置已经填好的测试编译器）+ `-Wall`。这也解释了 §4.1 里「禁用裸写 `clang`、必须用 `%clang`」的原因——`%clang` 自带正确编译器和额外标志。

> 注意：builtins 的功能测试主要放在 `test/builtins/TestCases/`（多为平台相关，如 Darwin 的 `os_version_check_test.c`）和 `test/builtins/Unit/`（大量「编译某内置函数 + 链接库 + 跑断言」的测试）。`Unit/` 下还有自己的 [test/builtins/Unit/lit.cfg.py](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/lit.cfg.py)，额外定义了 `%clang_builtins` 和 `%librt`。

**(c) builtins Unit 配置：`%librt` 与 `librt_has_*`**

Unit 测试要链接 compiler-rt 的静态库，于是有 `%librt` 替换符，按平台拼出正确的库路径与依赖：

[test/builtins/Unit/lit.cfg.py:56-96](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/lit.cfg.py#L56-L96)（节选）

```python
else:
    base_lib = os.path.join(
        config.compiler_rt_libdir, "libclang_rt.builtins%s.a" % config.target_suffix)
    ...
    if linker_supports_start_group and "nvptx" not in config.target_arch:
        config.substitutions.append(
            ("%librt ", "-lm -Wl,--start-group " + base_lib + " -lc -Wl,--end-group "))
    else:
        config.substitutions.append(("%librt ", "-lm " + base_lib + " -lc "))
```

中文说明：`%librt` 在 Linux 上展开成「`-lm` + `libclang_rt.builtins-<arch>.a` + `-lc`」，必要时用 `-Wl,--start-group/--end-group` 解决循环依赖。于是测试的 RUN 行 `%clang_builtins %s %librt -o %t` 就能编译并链接出可执行文件。

更巧妙的是 `librt_has_<函数名>` 特性——它由 CMake **按当前架构实际编译进库的源文件**自动生成：

[test/builtins/CMakeLists.txt:166-179](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/CMakeLists.txt#L166-L179)（节选）

```cmake
foreach (file_name ${BUILTIN_LIB_SOURCES})
    # 去掉目录和扩展名，如 "hexagon/udivsi3.S" => "udivsi3"
    get_filename_component(FILE_NAME_FILTERED "${file_name}" NAME_WE)
    ...
    list(APPEND BUILTINS_LIT_SOURCE_FEATURES "librt_has_${_function}")
endforeach()
```

中文说明：库里有 `divdi3.c`，就生成特性 `librt_has_divdi3`；库里有 `udivsi3.S`，就生成 `librt_has_udivsi3`。这些特性最终被 [test/builtins/Unit/lit.cfg.py:170-191](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/lit.cfg.py#L170-L191) 写进 `config.available_features`。

于是测试可以这样写「前提条件」：

[test/builtins/Unit/divdi3_test.c:1-2](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/divdi3_test.c#L1-L2)

```c
// RUN: %clang_builtins %s %librt -o %t && %run %t
// REQUIRES: librt_has_divdi3
```

中文说明：只有当「这个架构的库里真的编进了 `__divdi3`」时（特性 `librt_has_divdi3` 存在），测试才会运行；否则被 lit 跳过，而不是误报失败。这是一个非常优雅的「能力探测 → 条件运行」模式。

#### 4.2.4 代码实践

**实践目标**：读懂 builtins 测试的 RUN 行，理解 `%clang` / `%clang_builtins` / `%librt` / `%run` 各自展开成什么。

**操作步骤**：

1. 打开 [test/builtins/Unit/divdi3_test.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/divdi3_test.c)，读它第 1–2 行的 RUN / REQUIRES。
2. 对照本节源码，把 RUN 行 `// RUN: %clang_builtins %s %librt -o %t && %run %t` 里的四个占位符逐一展开（写出它们最终变成的真实命令片段）。
3. 解释为什么需要 `REQUIRES: librt_has_divdi3`——把答案和 [test/builtins/CMakeLists.txt:166-179](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/CMakeLists.txt#L166-L179) 对应起来。

**需要观察的现象 / 预期结果**（可在你的构建目录里实测）：

- 用 `llvm-lit` 单跑该测试并打印详细输出：
  ```bash
  llvm-lit -a <build>/test/builtins/Unit/divdi3_test.c
  ```
  `-a`（`--show-all`）会打印每条 RUN 行**替换后**的真实命令和退出码。

**预期结果**：你能在 `-a` 输出里看到 `%clang_builtins` 已被替换成「真实 clang + target_cflags + 调试标志」，`%librt` 被替换成「`-lm libclang_rt.builtins-<arch>.a -lc`」，`%run` 被替换成空串。如果当前架构库不含 `__divdi3`，则该测试显示为 `Unsupported`（被 `REQUIRES` 跳过），而不是 `FAIL`。

> 若你尚未构建、无法运行 `llvm-lit`，本实践可降级为「源码阅读型」：手工把替换符展开写在纸上即可，结论一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `test/builtins/lit.cfg.py` 里要定义 `%clang`，而公共配置里却「禁止裸写 clang」？

**参考答案**：公共配置把 `" clang"` 替换成报错文本以阻止裸写；`%clang` 的具体定义必须由各套件给出，因为不同套件需要不同的额外标志（builtins 加 `-Wall`，Unit 还要 `-fno-builtin -I<源码>` 等）。这样既强制统一入口，又允许套件定制。

**练习 2**：`REQUIRES: librt_has_divdi3` 失败时，测试结果是什么？

**参考答案**：是 `Unsupported`（被跳过），不计为失败。因为这些特性是 CMake 按「当前架构库里实际编译了哪些函数」生成的；架构库里没有 `__divdi3` 时，强行跑该测试没有意义，跳过才是正确语义。

---

### 4.3 目标三元组与 REQUIRES/XFAIL

#### 4.3.1 概念说明

compiler-rt 的测试要跑在**很多架构**上（x86_64、i386、aarch64、arm、mips 系列、riscv、powerpc……）。同一次构建经常**同时测多个架构**（例如 `x86_64-linux-gnu` 主机上会同时跑 x86_64 和 i386 的测试）。这给「限制测试只在某架构跑」带来两个著名陷阱，官方文档专门警告。

三个指令的区别：

| 指令 | 含义 |
|---|---|
| `REQUIRES: <feature>` | 仅当列出的特性**全部可用**时才运行测试；否则跳过（`Unsupported`） |
| `UNSUPPORTED: <feature>` | 列出的特性**任一可用**就跳过测试 |
| `XFAIL: <feature>` | 列出的特性**任一可用**时，测试即使失败也算「预期失败」（`Expected Failure`），不报红 |

#### 4.3.2 核心流程

compiler-rt 推荐的「按架构限制」做法是用 `target-is-${arch}` 特性串，流程是：

```
模板 lit.common.configured.in 第 63 行：
   config.available_features.add('target-is-%s' % config.target_arch)
        │  （每个架构变体只添加自己这一个 target-is-<arch>，精确匹配）
        ▼
测试里写：
   REQUIRES: target-is-x86_64      ← 只在 x86_64 变体运行
   XFAIL:    target-is-powerpc64le ← 在 ppc64le 变体上标记预期失败
```

为什么不直接用 `target=<三元组正则>`？见下面源码精读里的两个陷阱。

#### 4.3.3 源码精读

**(a) 文档警告的两个陷阱**

[docs/TestingGuide.md:25-46](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/docs/TestingGuide.md#L25-L46) 给出两个陷阱：

```text
陷阱一：正则会误伤更多三元组。
  XFAIL: target=mips{{.*}} 会同时命中 mips-linux-gnu、mipsel-linux-gnu、
  mips64-linux-gnu、mips64el-linux-gnu。

陷阱二：默认目标三元组对 compiler-rt 往往不合适，
因为 compiler-rt 一次会为多个目标编译。
  在 x86_64-linux-gnu 主机上，XFAIL: target=x86_64{{.*}} 会把 x86_64
  和 i386 两个变体的测试都标记为预期失败；而 XFAIL: target=i386{{.*}}
  则毫无作用（因为默认三元组是 x86_64）。
```

中文说明：用 `target=...` 的正则去匹配三元组，要么匹配过宽（mips 家族全中招），要么在「一次测多架构」时失效（i386 想单独标失败却匹配不到）。

**(b) 官方解法：`target-is-${arch}` 精确特性**

[docs/TestingGuide.md:48-64](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/docs/TestingGuide.md#L48-L64)

```text
compiler-rt 提供形如 target-is-${arch} 的特性串，可精确指定单个目标。
${arch} 取自 CMake 输出里的这些行：
  -- Compiler-RT supported architectures: x86_64;i386
  -- Builtin supported architectures: i386;x86_64
例如 XFAIL: target-is-x86_64 只让 x86_64 变体预期失败，不影响 i386；
XFAIL: target-is-i386 即便默认三元组是 x86_64-linux-gnu 也能命中 i386。
这些串要求精确匹配：target-is-mips / target-is-mipsel /
target-is-mips64 / target-is-mips64el 各自指代不同的 MIPS 目标。
```

而生成这个特性的代码就在模板里——**每个架构变体的配置只添加属于自己的那一个**：

[test/lit.common.configured.in:63](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.configured.in#L63)

```python
config.available_features.add('target-is-%s' % config.target_arch)
```

中文说明：因为每个变体的 `config.target_arch` 不同（x86_64 变体加 `target-is-x86_64`，i386 变体加 `target-is-i386`），所以 `target-is-*` 能精确到单个架构变体，绕开了「正则误伤」和「多架构同测」两个陷阱。

**(c) 真实测试里的用法**

`target-is-*` 的精确性在实战中被广泛使用：

[test/builtins/Unit/ppc/fixtfdi_test.c:2](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/ppc/fixtfdi_test.c#L2)

```c
// REQUIRES: target-is-powerpc64le
```

而「老的」`target=` 正则写法仍散见于 sanitizer / profile 测试，可作为「陷阱」的活样本：

[test/profile/coverage-inline.cpp:1](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/profile/coverage-inline.cpp#L1)

```c++
// XFAIL: target={{.*}}-aix{{.*}}
```

[test/sanitizer_common/TestCases/Posix/illegal_read_test.cpp:7](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/sanitizer_common/TestCases/Posix/illegal_read_test.cpp#L7)

```c++
// XFAIL: target={{(powerpc64|s390x).*}}
```

中文说明：这些 `target={{...}}` 用正则匹配三元组子串，在某些场景可行，但一旦碰到「一族架构名互为前缀」或「一次测多架构」，就会落入文档警告的陷阱——新写测试应优先用 `target-is-*`。

> 补充：除了架构特性，公共配置还提供 `target_os`（如 `linux`、`darwin`）、`<arch>-<os>`（如 `x86_64-linux`）、`compiler-rt-optimized`、`glibc-2.34`、`page-size-4096` 等大量特性，可按需在 `REQUIRES` 里组合（见 [test/lit.common.cfg.py:305-311](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L305-L311)、[test/lit.common.cfg.py:517-522](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L517-L522) 与 §4.1 (e)）。

#### 4.3.4 代码实践

**实践目标**：用一次测试运行，亲眼看到「同一架构只在对应变体上跑」。

**操作步骤**：

1. 在构建目录跑一个内置了 `REQUIRES: target-is-*` 的测试，并用 `-v` 看它在哪些变体运行、在哪些变体被跳过：
   ```bash
   llvm-lit -v <build>/test/builtins/Unit/ppc/fixtfdi_test.c
   ```
2. 在你的主机架构上（多半不是 powerpc64le），该测试应显示为 `Unsupported`。
3. 翻 [test/builtins/Unit/ppc/](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/ppc/) 下任意一个测试，确认它们都以 `REQUIRES: target-is-powerpc64le` 开头。

**需要观察的现象 / 预期结果**：

- 在非 ppc64le 主机上，`fixtfdi_test.c` 显示 `Unsupported`（因为 `target-is-powerpc64le` 特性不存在），而非 `FAIL`。
- 这正是 `target-is-*` 精确匹配的价值：它不会像 `target=powerpc{{.*}}` 那样误伤 `powerpc`/`powerpc64`/`powerpc64le` 一整族。

> 若无法运行（无构建目录），降级为阅读型实践：对比 `REQUIRES: target-is-powerpc64le`（精确）与 `XFAIL: target={{(powerpc64|s390x).*}}`（正则宽匹配），各自说明其语义与潜在风险即可。

#### 4.3.5 小练习与答案

**练习 1**：在 `x86_64-linux-gnu` 主机上同时测 x86_64 与 i386。若某测试只在 x86_64 上失败，应写成 `XFAIL: target=x86_64{{.*}}` 还是 `XFAIL: target-is-x86_64`？为什么？

**参考答案**：应写 `XFAIL: target-is-x86_64`。因为 `target=x86_64{{.*}}` 会同时把 x86_64 和 i386 两个变体都标记为预期失败（陷阱二），而 `target-is-x86_64` 是精确特性，只影响 x86_64 变体，不影响 i386。

**练习 2**：`REQUIRES: target-is-mips`、`REQUIRES: target-is-mipsel`、`REQUIRES: target-is-mips64` 三者是什么关系？这相比 `target=mips{{.*}}` 有何改进？

**参考答案**：三者各自是**独立的精确特性**，分别只命中 `mips`、`mipsel`、`mips64` 一个变体，互不影响。而 `target=mips{{.*}}` 用正则子串匹配，会同时命中整个 mips 家族（陷阱一），无法只挑其中一个。

---

## 5. 综合实践

设计一个贯穿三个模块的小任务：**给 builtins 套件新增一个最小的 lit 测试，验证它被正确发现、替换、运行，并体会 `REQUIRES` 的跳过语义。**

> 说明：以下「新增文件」是在**你自己的本地工作副本**里做练习，完成后请删除，不要提交到仓库，也不要污染项目的真实源码树。

**步骤 1：理解被复用的替换符。** 阅读本讲 §4.2，确认 `%clang`、`%run` 由谁定义、展开成什么。

**步骤 2：新建一个最小测试。** 在 `test/builtins/TestCases/` 下新建 `mini_lit_demo.c`（这是源码阅读 + 动手验证型实践）：

```c
// 这是「示例代码」，仅用于学习 lit 测试结构，不属于项目原有测试。
// RUN: %clang %s -o %t && %run %t
// REQUIRES: native-run

#include <stdio.h>
int main(void) {
  printf("hello from lit demo\n");
  return 0;
}
```

要点解释：

- `%clang` 由 [test/builtins/lit.cfg.py:19-22](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/lit.cfg.py#L19-L22) 定义；`%run` 由公共配置 [test/lit.common.cfg.py:492-498](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L492-L498) 定义（本机为空串）。
- `%t` 是 lit 约定的「临时输出文件路径」占位符。
- `REQUIRES: native-run`：`native-run` 特性在「非模拟器、能本机跑」时由 [test/builtins/Unit/lit.cfg.py:167-168](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/lit.cfg.py#L167-L168) 添加（注意：该特性目前只在 Unit 子套件注册；若你在 `TestCases/` 下用不到它，可改用 `REQUIRES: linux` 之类由公共配置注册的特性，见 §4.1 (e)）。

**步骤 3：验证它被 lit 收集。** 让 lit 只列出套件、不执行：

```bash
llvm-lit --show-suites <build>/test/builtins
llvm-lit -v <build>/test/builtins/TestCases/mini_lit_demo.c
```

**步骤 4：观察替换结果。** 用 `-a` 看替换后的真实命令：

```bash
llvm-lit -a <build>/test/builtins/TestCases/mini_lit_demo.c
```

**步骤 5：体会 REQUIRES 跳过。** 把 `REQUIRES:` 改成一个当前环境不存在的特性（如 `REQUIRES: target-is-powerpc64le`），再跑一次，观察它变成 `Unsupported`（被跳过），而非执行。

**需要观察的现象 / 预期结果**：

- `--show-suites` 能看到 `Builtins` 套件及其 `Test suffixes` 包含 `.c`，说明你的文件会被当作测试。
- `-v` 输出里能看到 `PASS`，且 `-a` 输出里 `%clang` / `%run` / `%t` 都已被替换成真实字符串。
- 改成不存在的 `REQUIRES` 后，状态变为 `Unsupported`。

> 待本地验证：以上命令的实际输出取决于你是否已完成 [u1-l3](u1-l3-build-system.md) 的构建。若尚未构建，可仅完成步骤 1、2 的「文件结构与替换符分析」，同样能达成理解目标。**练习结束后请删除你新建的 `mini_lit_demo.c`。**

---

## 6. 本讲小结

- compiler-rt 的测试由 **lit** 以 **ShTest**（命令行式）格式执行：每个测试文件用 `// RUN: ...` 声明命令，lit 替换占位符后按退出码判定（[test/lit.common.cfg.py:113-114](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L113-L114)）。
- 配置是**两层结构**：CMake 模板 `lit.common.configured.in` 注入构建期变量 → `lit.common.cfg.py` 写公共逻辑；胶水 `lit.site.cfg.py.in` 再加载子套件 `lit.cfg.py`（[test/builtins/lit.site.cfg.py.in:4-7](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/lit.site.cfg.py.in#L4-L7)）。
- 资源目录靠调用 `clang -print-resource-dir` / `-print-runtime-dir` 定位，必要时用 `-resource-dir=` + `-Wl,-rpath=` 强制覆盖（[test/lit.common.cfg.py:26-65](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L26-L65)、[176-216](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.cfg.py#L176-L216)）。
- 子套件 `lit.cfg.py` 负责套件特有规则：后缀、`%clang`、`%librt` 等；builtins 还用 CMake 自动生成 `librt_has_<函数>` 特性，实现「按库内容条件运行」（[test/builtins/CMakeLists.txt:166-179](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/CMakeLists.txt#L166-L179)）。
- 限制测试按架构时，优先用精确的 `target-is-${arch}` 特性（[test/lit.common.configured.in:63](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/lit.common.configured.in#L63)），避开 `target=...` 正则的两个陷阱（[docs/TestingGuide.md:25-64](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/docs/TestingGuide.md#L25-L64)）。
- 运行测试：`cmake --build . --target check-builtins`（单套件）或 `check-compiler-rt`（全量）；直接用 `llvm-lit [-v|-a] <测试路径>` 跑单个文件并查看替换后的命令。

---

## 7. 下一步学习建议

- **进入 sanitizer 公共底座**：本讲让你能「跑测试」。下一站建议学 [u3-l1 sanitizer_common 概览与初始化](u3-l1-sanitizer-common-overview.md)，因为大多数 sanitizer 测试都依赖 `lit.common.cfg.py` 注册的 sanitizer 选项与符号化器替换符。
- **写一个真正的 sanitizer 测试**：阅读 `test/asan/TestCases/` 下的若干 `.cpp`，观察它们如何用 `%clangxx_asan`、`%run`、`FileCheck` 与 `REQUIRES` 组合出「触发错误并校验报告」的测试——这是本讲 ShTest 模式的进阶用法。
- **深入构建接线**：若你想理解「CMake 如何把每个套件变成 `check-<套件>` 目标」，可精读 [test/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/CMakeLists.txt) 的 `compiler_rt_test_runtime` 函数（[第 81-124 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/CMakeLists.txt#L81-L124)），这会与 [u1-l3 构建系统](u1-l3-build-system.md) 形成闭环。
- **可选**：了解 lit 的「外层」组织——`umbrella_lit_testsuite_begin/end(check-compiler-rt)`（[test/CMakeLists.txt:58](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/CMakeLists.txt#L58)、[183](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/CMakeLists.txt#L183)）如何把所有套件聚合成一个总目标。
