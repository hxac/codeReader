# NVCC 与 NVRTC 编译器对比

## 1. 本讲目标

本讲是「JIT 编译系统」单元的第四篇，承接 [u3-l1](u3-l1-jit-arch-overview.md)（JIT 架构总览）、[u3-l2](u3-l2-codegen-template-instantiation.md)（代码生成）、[u3-l3](u3-l3-jit-cache-include-hash.md)（缓存与头文件哈希）。前三讲已经讲清了 JIT 的总骨架：宿主 `generate_impl` 用 `fmt::format` 拼出一段 `.cu` 源码，交给 `compiler->build(name, code)` 编译、缓存、加载。但 `build()` 内部那个 `compile()` 究竟是**怎么编译**的，一直被当作黑盒。本讲打开这个黑盒，回答：

> **DeepGEMM 提供了两条编译后端——调用外部 `nvcc` 进程，或调用进程内的 NVRTC 库。它们各自怎么工作？各自组装了哪些编译开关？又如何根据编译器版本决定 GPU 目标架构后缀（`90a` / `100a` / `100f`）？**

学完本讲，你应当能够：

1. 说清 NVCCCompiler 与 NVRTCCompiler 两条后端的实现差异——一个 `fork` 出外部进程，一个在进程内调用 NVRTC API；
2. 解释两条后端各自组装的编译 flag（`--register-usage-level=10`、`--expt-extended-lambda`、`--pch`、`--device-int128` 等）分别做什么；
3. 看懂 `device_runtime->get_arch(number_only, support_arch_family)` 如何根据**编译器版本是否 ≥ 12.9** 决定 SM100 上使用 `100a` 还是 `100f` 架构家族后缀，以及 `DG_JIT_USE_NVRTC` 如何在两者间切换。

## 2. 前置知识

在进入源码前，先用通俗语言建立两对概念。

**NVCC 与 NVRTC。** 它们都是 NVIDIA 官方把 `.cu` 源码编译成 GPU 可执行二进制（cubin）的工具，区别在于「运行形态」：

- **NVCC（NVIDIA CUDA Compiler）** 是一个**独立的外部命令行程序**（通常在 `$CUDA_HOME/bin/nvcc`）。要编译一段源码，必须启动一个新的 `nvcc` 进程，把源码文件路径交给它，等它跑完再把产物 cubin 读回来。它是 CUDA 生态里最成熟、最稳定的编译器，但「启动一个进程」本身有开销。
- **NVRTC（NVIDIA Runtime Compilation）** 是一个**链接进你自己进程的库**（`libnvrtc`）。编译不需要启动外部进程，直接在你的进程里调用 `nvrtcCreateProgram` / `nvrtcCompileProgram` 这组 C API，源码以字符串形式传入，cubin 也以字符串形式直接返回。省去了进程启动开销，还支持预编译头（PCH），因此**编译速度显著更快**（README 称最高约 10 倍）。

**编译 flag 与目标架构。** 不管用哪个后端，编译时都要告诉编译器两件事：一是**怎么编译**（用哪个 C++ 标准、开启哪些语言扩展、限制多少寄存器），二是**给谁编译**（目标 GPU 架构）。后者用一个形如 `sm_90a`、`sm_100a`、`sm_100f` 的字符串表达，其中后缀 `a` 表示「该架构的全部特性（含 tensor core）」，`f` 表示「整个架构家族（family）」。本讲会看到，这个后缀的选择**不是固定的**，而是随编译器版本动态决定。

如果你对「设备 kernel 是 `.cuh` 模板、运行时才实例化编译」这一 JIT 哲学还不熟，建议先回顾 [u3-l1](u3-l1-jit-arch-overview.md) 与 [u3-l2](u3-l2-codegen-template-instantiation.md)。

## 3. 本讲源码地图

本讲聚焦两个文件，它们正好对应「两条后端」与「架构判定」：

| 文件 | 作用 | 本讲是否精读 |
|------|------|--------------|
| `csrc/jit/compiler.hpp` | `Compiler` 基类（公共 `build()` 与公共 flag）、`NVCCCompiler`、`NVRTCCompiler`，以及二选一的 `compiler` 全局对象 | ✅ 精读 |
| `csrc/jit/device_runtime.hpp` | `DeviceRuntime::get_arch()` / `get_arch_pair()`，决定 `sm_90a` / `sm_100a` / `sm_100f` | ✅ 精读 |

辅助理解的配套文件（本讲只引用，不精读）：

- `csrc/utils/lazy_init.hpp`：`LazyInit<T>` 模板，`compiler` 与 `device_runtime` 都靠它做「首次访问才构造」的惰性单例。
- `csrc/utils/system.hpp`：`get_env<T>()`（读环境变量并按类型转换）、`call_external_command()`（用 `popen` 跑外部命令并收集输出）——NVCC 后端就靠它调用 `nvcc` 进程。
- `csrc/utils/exception.hpp`：`DG_HOST_ASSERT`（失败抛 `DGException`，而非 abort）、`DG_NVRTC_CHECK`（封装 NVRTC API 的返回码检查）。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先讲两条编译后端各自的实现与取舍，再讲横跨两者的「版本检查与架构后缀」逻辑。

### 4.1 NVCC 后端：调用外部编译器进程

#### 4.1.1 概念说明

NVCC 后端是 DeepGEMM 的**默认后端**。它的核心思想极其朴素：既然 `nvcc` 已经是成熟工具，那就**把生成好的 `.cu` 源码写到磁盘，启动一个 `nvcc` 进程去编译，再把产物 cubin 读回来**。这样做的好处是「稳」——`nvcc` 的前端优化与 `ptxas` 调度经过长期打磨，对各种 kernel 形状都能产出质量稳定的机器码；代价是「慢」——每次 JIT 都要 `fork` 一个进程、启动编译器、读回文件，进程启动与磁盘 I/O 的固定开销不可忽略。

DeepGEMM 把这个后端实现为 `Compiler` 基类的派生类 `NVCCCompiler`，重写唯一的纯虚函数 `compile()`。

#### 4.1.2 核心流程

NVCC 后端的一次编译流程：

1. **写源码**：把 `build()` 传进来的 `code` 字符串写入临时目录的 `kernel.cu`。
2. **构造命令**：拼接 `cd {临时工作目录} && {nvcc路径} {源码} -cubin -o {cubin路径} {flags}`。之所以要先 `cd` 到一个干净的临时目录，注释里说明了原因——避免当前工作目录下的文件**遮蔽 C++ 标准库头文件**。
3. **执行外部命令**：用 `call_external_command()`（底层 `popen`）跑这条命令，收集返回码与输出。
4. **检查结果**：返回码非 0 则断言失败（抛 `DGException`）；若开启 `DG_JIT_PTXAS_CHECK`，还要检查输出里有没有 `Local memory used`（寄存器溢出到 local memory 视为性能隐患）。
5. **可选产出 PTX**：如果调用方要求 PTX（如 `DG_JIT_DUMP_PTX`），再额外跑一遍 `-ptx` 编译。

其中第 2 步的 `flags` 字符串，是 NVCC 后端在**构造函数**里就拼好的——这部分是理解「编译开关」的关键。

#### 4.1.3 源码精读

**构造函数：定位 nvcc、读版本、拼 flag。** NVCC 后端在构造时完成三件事，见 [csrc/jit/compiler.hpp:L194-L209](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L194-L209)：

```cpp
NVCCCompiler() {
    nvcc_path = cuda_home / "bin" / "nvcc";
    if (const auto env_nvcc_path = get_env<std::string>("DG_JIT_NVCC_COMPILER"); not env_nvcc_path.empty())
        nvcc_path = env_nvcc_path;                 // 允许用环境变量覆盖 nvcc 路径
    const auto [nvcc_major, nvcc_minor] = get_nvcc_version();   // 读 nvcc --version
    signature = fmt::format("NVCC{}.{}", nvcc_major, nvcc_minor);

    // Only NVCC >= 12.9 supports arch-specific family suffix
    const auto arch = device_runtime->get_arch(false, nvcc_major > 12 or nvcc_minor >= 9);
    flags = fmt::format("{} -I{} --gpu-architecture=sm_{} "
                        "--compiler-options=-fPIC,-O3,-fconcepts,-Wno-deprecated-declarations,-Wno-abi "
                        "-O3 --expt-relaxed-constexpr --expt-extended-lambda",
                        flags, library_include_path.c_str(), arch);
}
```

- `nvcc_path` 默认取 `$CUDA_HOME/bin/nvcc`，可用 `DG_JIT_NVCC_COMPILER` 覆盖——方便在容器里指定特定版本的 nvcc。
- `get_nvcc_version()` 见 [csrc/jit/compiler.hpp:L174-L191](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L174-L191)：执行 `nvcc --version`，用正则 `release (\d+\.\d+)` 抓出版本号，并断言 **≥ 12.3**（低于会抛异常）；若 `< 12.9` 还会打印一条「建议升级到 12.9 以获得最佳性能」的警告。
- `signature` 被设成 `NVCC{major}.{minor}`（如 `NVCC12.9`）。这个 signature 会进入缓存键（见 u3-l3），所以**换一个 nvcc 版本，旧缓存会自动失效重编**。
- 第二个参数 `nvcc_major > 12 or nvcc_minor >= 9` 即 `support_arch_family`，它决定 `get_arch` 返回 `100a` 还是 `100f`（详见 4.3）。
- `flags` 在基类公共 flag 之上**追加**了 NVCC 特有的开关（`--gpu-architecture`、`-fPIC`、`--expt-extended-lambda` 等）。

**公共 flag（两条后端共享）。** 上面看到的 `flags` 在追加前并非空——它在基类 `Compiler` 构造函数里就已经有一份「所有派生编译器都生效」的公共 flag，见 [csrc/jit/compiler.hpp:L53-L62](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L53-L62)：

```cpp
signature = "unknown-compiler";
flags = fmt::format("-std=c++{} --diag-suppress=39,161,174,177,186,940 "
                    "--ptxas-options=--register-usage-level=10",
                    get_env<int>("DG_JIT_CPP_STANDARD", 20));
if (get_env("DG_JIT_DEBUG", 0) or get_env("DG_JIT_PTXAS_VERBOSE", 0) or get_env("DG_JIT_PTXAS_CHECK", 0))
    flags += " --ptxas-options=--verbose,--warn-on-local-memory-usage";
if (get_env("DG_JIT_WITH_LINEINFO", 0))
    flags += " -Xcompiler -rdynamic -lineinfo";
```

几个要点：

| 开关 | 含义 |
|------|------|
| `-std=c++{N}` | C++ 标准，由 `DG_JIT_CPP_STANDARD` 控制，**默认 20**。设备模板大量用了 C++20 概念与 `consteval`。 |
| `--diag-suppress=39,161,...` | 压制一批无伤大雅的编译警告编号，避免日志噪音。 |
| `--ptxas-options=--register-usage-level=10` | 传给底层 `ptxas` 的寄存器使用上限旋钮，**两条后端共享**。它影响 kernel 的寄存器占用与 occupancy（其精确数值语义属 ptxas 实现细节，可在 u10-l4 用 `cuobjdump` 观察实际占用）。 |
| `--warn-on-local-memory-usage` | 调试模式下，一旦 kernel 把数据溢出到 local memory 就告警（与 `DG_JIT_PTXAS_CHECK` 配合可断言失败）。 |
| `-lineinfo` | 由 `DG_JIT_WITH_LINEINFO` 控制，嵌入源码行号信息，供 NCU/cuobjdump 等剖析工具定位热点（见 u10-l4）。 |

**compile()：写文件、跑 nvcc、读结果。** 真正的编译动作见 [csrc/jit/compiler.hpp:L211-L251](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L211-L251)，关键三行：

```cpp
const auto code_path = dir_path / "kernel.cu";
put(code_path, code);                                  // 写源码到磁盘
const auto compile_dir = make_tmp_dir();               // 干净工作目录，避免遮蔽标准库头
const auto command = fmt::format("cd {} && {} {} -cubin -o {} {}",
    compile_dir.c_str(), nvcc_path.c_str(), code_path.c_str(), cubin_path.c_str(), flags);
const auto [return_code, output] = call_external_command(command);   // fork + popen
```

注意 `-cubin`：直接产出 GPU 二进制 cubin（而非先出 PTX）。如果调用方还想要 PTX，会在 [csrc/jit/compiler.hpp:L232-L242](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L232-L242) 额外跑一遍 `-ptx` 编译。

#### 4.1.4 代码实践

**实践目标**：让 NVCC 后端「自报家门」，观察它实际执行的那条 `nvcc` 命令，理解 flag 是如何拼出来的。

**操作步骤**：

1. 确认环境变量 `DG_JIT_USE_NVRTC` 未设置或为 `0`（默认即 NVCC）。
2. 编写一个只跑一次最小 GEMM 的脚本 `t.py`（参照 `tests/test_fp8_fp4.py` 的写法）：

   ```python
   # 示例代码：仅用于触发一次 JIT 编译，形状可按本机显存调整
   import torch, deep_gemm
   from tests.generators import generate_m_grouped_contiguous  # 复用现成生成器，或自行构造

   M, N, K = 4096, 4096, 512
   a, a_sf = (torch.randn(M, K, device='cuda').to(torch.float8_e4m3fn), None)
   b, b_sf = (torch.randn(N, K, device='cuda').to(torch.float8_e4m3fn), None)
   # 注意：真实调用需按 u2-l2 准备逐块缩放因子 SF，这里仅示意触发编译
   d = torch.empty(M, N, device='cuda', dtype=torch.bfloat16)
   # deep_gemm.fp8_gemm_nt((a, a_sf), (b, b_sf), d)   # 取消注释并补全 SF 后即可触发
   ```

3. 带上打印编译命令的开关运行：

   ```bash
   DG_JIT_USE_NVRTC=0 DG_JIT_PRINT_COMPILER_COMMAND=1 python t.py
   ```

**需要观察的现象**：控制台应打印一行 `Running NVCC command: cd ... && /.../nvcc .../kernel.cu -cubin -o .../kernel.cubin -std=c++20 --diag-suppress=... --ptxas-options=--register-usage-level=10 -I.../include --gpu-architecture=sm_90a ... --expt-extended-lambda`（SM100 机器上 `sm_90a` 会变成 `sm_100a` 或 `sm_100f`）。

**预期结果**：你能在这条命令里逐一找到 4.1.3 讲到的公共 flag（`-std=c++20`、`--register-usage-level=10`）与 NVCC 专属 flag（`--gpu-architecture`、`--expt-extended-lambda`），从而把「源码里的 `flags` 字符串」与「真实传给 nvcc 的命令行」对应起来。本机若暂无 SM90/SM100 GPU，则「待本地验证」——可改为只读源码，对照 4.1.3 的拼接逻辑手写一遍这条命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NVCC 后端的 `compile()` 要先 `cd` 到一个 `make_tmp_dir()`，而不是直接在工作目录编译？

**答案**：源码注释 [csrc/jit/compiler.hpp:L219](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L219) 写明：避免当前工作目录下的文件**遮蔽 C++ 标准库头文件**（cwd 会被加入头文件搜索路径，若 cwd 里有同名文件会导致错误的 include）。

**练习 2**：把 `DG_JIT_NVCC_COMPILER` 指向一个不存在的路径，会发生什么？

**答案**：`get_nvcc_version()` 里 [csrc/jit/compiler.hpp:L175](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L175) 会先 `DG_HOST_ASSERT(std::filesystem::exists(nvcc_path))`，构造 `NVCCCompiler` 时即抛 `DGException`（经 pybind11 翻译为 Python 异常），`compiler` 单例无法初始化。

### 4.2 NVRTC 后端：进程内编译与 PCH

#### 4.2.1 概念说明

NVRTC 后端是**可选的加速后端**，由 `DG_JIT_USE_NVRTC=1` 开启。它与 NVCC 后端完成的是**同一件事**（把 `.cu` 编译成 cubin），但实现方式截然不同：它**不启动任何外部进程**，而是把 NVRTC 库链接进 `_C` 扩展里，直接在进程内调用 `nvrtcCreateProgram` / `nvrtcCompileProgram` 这组 C API。

这样做的好处是编译速度——README 称最高约 10 倍提升。提速来自两点：一是省掉了每次 `fork` nvcc 进程的固定开销；二是 NVRTC 支持**预编译头（PCH, Precompiled Header）**，可以把庞大的 `<deep_gemm/*>` 设备头文件预编译一次、后续编译直接复用，避免对同一批头文件反复解析。代价是 README 也坦承：NVRTC 在**某些 case 下产出的 kernel 性能可能略差**。其根因属于 nvcc 与 nvrtc 前端/调度实现的历史差异，源码未详细说明，建议用 `DG_JIT_DUMP_SASS=1` 对比两份汇编来定位（见 [u10-l4](u10-l4-env-vars-debug-profiling.md)）。

#### 4.2.2 核心流程

NVRTC 后端的一次编译流程：

1. **写源码**：同样把 `code` 写入 `kernel.cu`（虽然 NVRTC 用字符串喂程序，但写盘是为了缓存可读性与 dump）。
2. **拆 flag**：把 `flags` 字符串按空白拆成 `options` 字符串数组，再转成 NVRTC 要求的 `const char*` 数组。
3. **建程序**：`nvrtcCreateProgram(&program, code, "kernel.cu", 0, nullptr, nullptr)`——用源码字符串创建一个 NVRTC 程序对象。
4. **编译**：`nvrtcCompileProgram(program, num_options, options)`——**在进程内**完成编译，返回码非 0 时读取日志并断言。
5. **取产物**：依次 `nvrtcGetPTX` / `nvrtcGetCUBIN` 直接拿到内存里的字节串，写回磁盘的 `kernel.cubin`（可选 `kernel.ptx`）。
6. **销毁**：`nvrtcDestroyProgram` 释放程序对象。

其中第 2 步的 `flags` 同样在构造函数里拼好，但**多了 PCH 与若干 NVRTC 专属开关**。

#### 4.2.3 源码精读

**构造函数：读版本、加 include 目录、配 PCH、拼 flag。** 见 [csrc/jit/compiler.hpp:L256-L282](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L256-L282)：

```cpp
NVRTCCompiler() {
    int major, minor;
    DG_NVRTC_CHECK(nvrtcVersion(&major, &minor));     // 进程内查询 NVRTC 版本，无需启动进程
    signature = fmt::format("NVRTC{}.{}", major, minor);
    DG_HOST_ASSERT((major > 12 or (major == 12 and minor >= 3)) and "NVRTC version should be >= 12.3");

    std::string include_dirs;
    include_dirs += fmt::format("-I{} ", library_include_path.string());
    include_dirs += fmt::format("-I{} ", (cuda_home / "include").string());

    // Add PCH support for version 12.8 and above
    // NOTES: PCH is vital for compilation speed
    std::string pch_flags;
    if (major > 12 or minor >= 8) {
        pch_flags = "--pch ";
        if (get_env<int>("DG_JIT_DEBUG", 0))
            pch_flags += "--pch-verbose=true ";
    }

    // Only NVRTC >= 12.9 supports arch-specific family suffix
    const auto arch = device_runtime->get_arch(false, major > 12 or minor >= 9);
    flags = fmt::format("{} {}--gpu-architecture=sm_{} -default-device {} --device-int128",
                        flags, include_dirs, arch, pch_flags);
}
```

对比 NVCC 后端，几处关键差异：

| 维度 | NVCC 后端 | NVRTC 后端 |
|------|-----------|-----------|
| 版本获取 | `nvcc --version`（启动进程 + 正则解析） | `nvrtcVersion()`（进程内 API，零开销） |
| signature | `NVCC{maj}.{min}` | `NVRTC{maj}.{min}` |
| include 目录 | 仅 `-I{library_include}` | **额外加** `-I{cuda_home/include}`（NVRTC 默认不含 CUDA 运行时头） |
| PCH | 不支持 | **≥12.8 启用 `--pch`**（注释明言「PCH 对编译速度至关重要」） |
| 专属开关 | `--expt-extended-lambda` 等 nvcc 风格 | `-default-device`、`--device-int128` |

两个 NVRTC 专属开关的含义：

- `-default-device`：把整个翻译单元当作设备代码编译，让 NVRTC 在没有显式 `__global__` 入口标注时也能正确处理设备函数（DeepGEMM 的 `.cu` 主要靠「取函数地址强制实例化模板」，见 u3-l2）。
- `--device-int128`：开启设备端 `__int128` 支持（部分设备代码用到 128 位整数运算）。

**compile()：纯进程内 API 调用。** 见 [csrc/jit/compiler.hpp:L284-L351](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L284-L351)，核心是建程序 + 编译 + 取产物：

```cpp
nvrtcProgram program;
DG_NVRTC_CHECK(nvrtcCreateProgram(&program, code.c_str(), "kernel.cu", 0, nullptr, nullptr));
const auto compile_result = nvrtcCompileProgram(program, (int)option_cstrs.size(), option_cstrs.data());
// ... 读取日志（失败时断言 log 非空）...
size_t cubin_size;
DG_NVRTC_CHECK(nvrtcGetCUBINSize(program, &cubin_size));
std::string cubin_data(cubin_size, '\0');
DG_NVRTC_CHECK(nvrtcGetCUBIN(program, cubin_data.data()));
put(cubin_path, cubin_data);                          // 字节串直接写盘
DG_NVRTC_CHECK(nvrtcDestroyProgram(&program));
```

注意：编译结果（PTX/CUBIN）是**直接以字符串形式拿到**的，不需要像 NVCC 后端那样去读一个外部进程产出的文件——这正是「进程内编译」的本质。所有 NVRTC 调用都被 `DG_NVRTC_CHECK` 包裹，返回码非 `NVRTC_SUCCESS` 即抛 `DGException`（见 `csrc/utils/exception.hpp`）。

**两条后端共享的 `build()`。** 值得强调的是：上面两个 `compile()` 都是 `Compiler` 基类**纯虚函数**的两种实现，而真正被调用方（如 `sm90_fp8_gemm_1d1d`）触发的 `build()` 是**基类公共方法**——签名计算、缓存查找、临时目录编译、`fsync`、原子重命名、写入运行时缓存这套流程（[csrc/jit/compiler.hpp:L100-L149](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L100-L149)，详见 u3-l1）对两条后端完全一致。两条后端只负责「把源码变成 cubin」这一步，其余基础设施是共享的。

#### 4.2.4 代码实践

**实践目标**：切换到 NVRTC 后端，观察它打印的编译选项，与 NVCC 后端对比 flag 差异。

**操作步骤**：

1. 用与 4.1.4 相同的 `t.py`，这次显式开启 NVRTC：

   ```bash
   DG_JIT_USE_NVRTC=1 DG_JIT_PRINT_COMPILER_COMMAND=1 python t.py
   ```

2. 在控制台找 `Compiling JIT runtime with NVRTC options: ...` 这一行。

**需要观察的现象**：该行会列出拆分后的每个选项，你应能看到 `-default-device`、`--device-int128`，以及（若 NVRTC ≥ 12.8）`--pch`；若是 SM100 且 NVRTC ≥ 12.9，还能看到 `--gpu-architecture=sm_100f`。

**预期结果**：把这一行选项与 4.1.4 的 `Running NVCC command` 行并排对比，你能直观看到两套 flag 的「同」（公共 flag 完全一致）与「异」（NVCC 的 `--expt-extended-lambda` vs NVRTC 的 `-default-device`/`--device-int128`/`--pch`）。本机若无 GPU，则「待本地验证」——可对照 4.1.3 与 4.2.3 的拼接逻辑，手写出两份 flag 字符串做文本对比。

#### 4.2.5 小练习与答案

**练习 1**：NVRTC 后端的 signature 与 NVCC 后端不同（`NVRTC12.9` vs `NVCC12.9`）。这会带来什么副作用？

**答案**：signature 进入缓存键（u3-l3 的 `get_hex_digest` 输入包含 signature）。所以在同一缓存目录下，**NVCC 与 NVRTC 编译出的同一 kernel 会落进不同的缓存子目录**（摘要不同），互不覆盖；切换后端会触发一次新的编译，但不会损坏另一边的缓存。

**练习 2**：注释里说「PCH is vital for compilation speed」。如果 NVRTC 版本只有 12.6（< 12.8），会发生什么？

**答案**：[csrc/jit/compiler.hpp:L271](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L271) 的 `major > 12 or minor >= 8` 为假，`pch_flags` 保持为空字符串，于是不开 PCH——能正常编译，但拿不到 PCH 带来的编译加速。

### 4.3 版本检查与 arch 后缀（90a / 100a / 100f）

#### 4.3.1 概念说明

两条后端都要回答一个问题：**这次编译的目标 GPU 架构字符串是什么？** 这个字符串形如 `sm_90a`（Hopper）、`sm_100a` 或 `sm_100f`（Blackwell），由 `--gpu-architecture=sm_{arch}` 传给编译器。其中后缀的含义：

- `a`（如 `sm_90a`、`sm_100a`）：编译针对**具体某一型号**，启用该型号的全部特性（含 tensor core 指令）。`a` = "all features"。
- `f`（如 `sm_100f`）：编译针对**整个架构家族**（family），产出的 cubin 可在该家族内多个型号上运行。`f` = "family"。

关键在于：SM100 上到底用 `100a` 还是 `100f`，**不是写死的，而是取决于编译器版本是否 ≥ 12.9**——因为「架构家族后缀」是 CUDA 12.9 才引入的能力。这个判定逻辑集中在 `DeviceRuntime::get_arch()`，两条后端都调用它，只是各自把「自己是否 ≥ 12.9」作为第二个参数传进去。

#### 4.3.2 核心流程

`get_arch` 的判定逻辑（伪代码）：

```
读取设备属性 (major, minor)
若 major == 10 且 minor != 1:            # 即标准 Blackwell SM100（非 10.1）
    若 number_only:        返回 "100"
    否则若 support_arch_family: 返回 "100f"   # 编译器 ≥ 12.9 才走这里
    否则:                  返回 "100a"
否则:                                      # SM90 等其它架构，或 SM101(10.1)
    返回 "{major*10+minor}" + (number_only ? "" : "a")
```

即：

- **SM90（major=9, minor=0）**：永远返回 `90a`，与编译器版本无关——Hopper 没有家族后缀一说。
- **SM100（major=10, minor=0）**：编译器 ≥ 12.9 → `100f`；否则 → `100a`。
- **SM101（major=10, minor=1）**：因为 `minor != 1` 不成立，走 else 分支 → `101a`，被当作独立型号处理，**不享受家族后缀**。

#### 4.3.3 源码精读

`get_arch` 见 [csrc/jit/device_runtime.hpp:L88-L97](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L88-L97)：

```cpp
std::string get_arch(const bool& number_only = false,
                     const bool& support_arch_family = false) {
    const auto [major, minor] = get_arch_pair();
    if (major == 10 and minor != 1) {
        if (number_only)
            return "100";
        return support_arch_family ? "100f" : "100a";
    }
    return std::to_string(major * 10 + minor) + (number_only ? "" : "a");
}
```

两个入参的含义：

- `number_only`：只返回数字部分（如 `100`），不带 `a`/`f` 后缀。本讲涉及的两处调用都传 `false`。
- `support_arch_family`：调用方是否「支持架构家族后缀」。**只有当编译器版本 ≥ 12.9 时才传 `true`**。

`get_arch_pair` 来自缓存的设备属性，见 [csrc/jit/device_runtime.hpp:L83-L86](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L83-L86)，它返回 `{prop->major, prop->minor}`（如 SM90 是 `{9,0}`，SM100 是 `{10,0}`）。

**两条后端如何调用它。** NVCC 后端在 [csrc/jit/compiler.hpp:L204](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L204) 传 `nvcc_major > 12 or nvcc_minor >= 9`；NVRTC 后端在 [csrc/jit/compiler.hpp:L279](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L279) 传 `major > 12 or minor >= 9`——表达式一模一样，只是各自用自己的版本号。两者都会先断言 **≥ 12.3**（NVCC 在 `get_nvcc_version` 里、NVRTC 在构造函数里）。

因此一个简洁的结论：**在 SM100 上，只要 NVCC 或 NVRTC 版本 ≥ 12.9，DeepGEMM 就用 `sm_100f`（家族后缀）编译；否则退回 `sm_100a`。** 这也是为什么两条后端都要读自己的版本——不仅为了写进 signature 影响缓存，还为了决定 arch 后缀。

**架构判定的更上层开关。** 别忘了，整个 `compiler` 对象本身是「二选一」的惰性单例，见 [csrc/jit/compiler.hpp:L354-L360](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L354-L360)：

```cpp
static auto compiler = LazyInit<Compiler>([]() -> std::shared_ptr<Compiler> {
    if (get_env<int>("DG_JIT_USE_NVRTC", 0)) {
        return std::make_shared<NVRTCCompiler>();
    } else {
        return std::make_shared<NVCCCompiler>();
    }
});
```

`LazyInit`（见 `csrc/utils/lazy_init.hpp`）保证：**首次有人写 `compiler->build(...)` 时**才真正构造编译器对象，且只构造一次。`DG_JIT_USE_NVRTC` 默认 `0`，所以**默认走 NVCC**。这个选择一旦在进程内确定，整个进程都用同一个后端——因为 `compiler` 是单例。

#### 4.3.4 代码实践

**实践目标**：搞清「自己的机器 + 自己的 CUDA 版本」下，DeepGEMM 实际用哪个 arch 后缀。

**操作步骤**：

1. 查本机 GPU 架构（major.minor）与 CUDA 版本：

   ```bash
   python -c "import torch; p=torch.cuda.get_device_properties(0); print('arch', p.major, p.minor); print('cuda', torch.version.cuda)"
   ```

2. 据此推断 `get_arch` 的返回值：若 `major==10 and minor==0`，则 CUDA ≥ 12.9 → `100f`，否则 → `100a`；若 `major==9` → 恒为 `90a`。

3. 用 4.1.4 / 4.2.4 的命令实际触发一次编译，在打印的命令行里确认 `--gpu-architecture=sm_???` 与你的推断一致。

**需要观察的现象 / 预期结果**：推断值与实际命令行里的 `sm_???` 应当完全吻合。若不吻合，最可能的原因是 NVCC 版本（`nvcc --version`）与 `torch.version.cuda` 不一致——注意 `get_nvcc_version` 读的是 **nvcc 本体**的版本，而非 PyTorch 报告的 CUDA 版本。本机无 GPU 时「待本地验证」，可改为在纸上对 [device_runtime.hpp:L88-L97](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L88-L97) 代入几组 `(major, minor, support_arch_family)` 手算。

#### 4.3.5 小练习与答案

**练习 1**：一台 SM100（10.0）机器，安装的 NVCC 是 12.6。DeepGEMM 会用什么 arch 后缀？为什么？

**答案**：`sm_100a`。因为 NVCC 12.6 < 12.9，`support_arch_family` 为 `false`，`get_arch(false, false)` 对 `(10,0)` 走 `return support_arch_family ? "100f" : "100a"` 分支，得 `100a`。升级到 12.9 后才会变成 `100f`。

**练习 2**：为什么 SM101（10.1）不走 `100f` 家族分支，而是返回 `101a`？

**答案**：`get_arch` 的条件是 `major == 10 and minor != 1`，SM101 的 `minor == 1` 使该条件为假，落到 else 分支返回 `101a`。即 DeepGEMM 只把「标准」SM100（10.0）纳入家族后缀处理，SM101 被当作独立的特定型号。

## 5. 综合实践

把本讲三个模块串起来，做一个「后端对比」实验：

1. 准备一个能稳定触发一次 JIT 编译的脚本（复用 `tests/test_fp8_fp4.py` 里任意一个真实 FP8 GEMM 用例，或 4.1.4 的 `t.py` 补全 SF 后的版本）。
2. **第一组（NVCC）**：清空缓存后计时

   ```bash
   DG_JIT_CACHE_DIR=/tmp/dg_nvcc rm -rf /tmp/dg_nvcc
   DG_JIT_CACHE_DIR=/tmp/dg_nvcc DG_JIT_USE_NVRTC=0 DG_JIT_DEBUG=1 \
     /usr/bin/time -v python t.py 2>&1 | grep -E 'Running NVCC|Elapsed|sm_1'
   ```

3. **第二组（NVRTC）**：换一个独立缓存目录，避免与第一组互相命中

   ```bash
   DG_JIT_CACHE_DIR=/tmp/dg_nvrtc rm -rf /tmp/dg_nvrtc
   DG_JIT_CACHE_DIR=/tmp/dg_nvrtc DG_JIT_USE_NVRTC=1 DG_JIT_DEBUG=1 \
     /usr/bin/time -v python t.py 2>&1 | grep -E 'NVRTC options|Elapsed|sm_1'
   ```

4. **对比三件事**：
   - **编译耗时**（`Elapsed` 墙钟时间）：NVRTC 应明显更短（尤其首次编译大量头文件时，PCH 优势显著）。
   - **打印的编译命令**：NVCC 是 `Running NVCC command: ...`，NVRTC 是 `Compiling JIT runtime with NVRTC options: ...`；确认两份命令里 `--gpu-architecture=sm_???` 相同（同一台机器、同一个 arch 后缀），但专属 flag 不同（`--expt-extended-lambda` vs `-default-device/--device-int128/--pch`）。
   - **产出的 kernel 性能**（可选，进阶）：用 `tests/testing/bench.py` 的测时工具对同一形状各跑一次，观察 TFLOPS 是否有差异；若 NVRTC 略慢，结合 `DG_JIT_DUMP_SASS=1` 对比两份 SASS 寻找原因（衔接 [u10-l4](u10-l4-env-vars-debug-profiling.md)）。

> 说明：因 NVRTC「某些 case 性能可能更差」的具体根因属于编译器实现差异，源码中未给出，故本步骤结论以「实测对比」为准——这正是本综合实践的价值。

## 6. 本讲小结

- DeepGEMM 的 JIT 有**两条编译后端**：默认的 `NVCCCompiler`（`fork` 外部 `nvcc` 进程，稳但慢）与可选的 `NVRTCCompiler`（进程内调用 NVRTC API，快、支持 PCH），由 `DG_JIT_USE_NVRTC` 在 `compiler` 这个 `LazyInit` 单例里二选一。
- 两条后端共享基类 `Compiler` 的**公共 flag**（`-std=c++20`、`--register-usage-level=10`、`--diag-suppress=...`、`-lineinfo`）与公共 `build()` 流程（签名→缓存→临时目录编译→`fsync`→原子重命名）；各自只重写 `compile()` 并追加专属 flag（NVCC 的 `--expt-extended-lambda`，NVRTC 的 `-default-device`/`--device-int128`/`--pch`）。
- 两条后端都会断言**编译器版本 ≥ 12.3**，NVCC 还会在 `< 12.9` 时警告；signature 被设成 `NVCC{x}.{y}` / `NVRTC{x}.{y}`，进入缓存键，故换版本或换后端都会触发重编且互不破坏对方缓存。
- 目标架构后缀由 `DeviceRuntime::get_arch(number_only, support_arch_family)` 决定：SM90 恒为 `90a`；SM100（10.0）在编译器 ≥ 12.9 时为 `100f`（家族后缀），否则为 `100a`；SM101（10.1）因 `minor==1` 走 else 分支返回 `101a`。
- NVRTC 编译虽快，但 README 与源码均坦承其在**某些 case 下产出的 kernel 性能可能略差**，根因属编译器实现差异、源码未细述，需用 SASS dump 实测对比。

## 7. 下一步学习建议

本讲把 JIT 编译系统单元的「编译后端」讲完了。建议接下来：

1. **进入宿主侧内核启动链路（Unit 4）**：编译产出 cubin 后，DeepGEMM 如何用 CUDA Driver/Runtime API 加载它、枚举 kernel 符号、组装 `LaunchConfig`（含 cluster、PDL 属性）并启动？见 [u4-l1 设备运行时与配置](u4-l1-device-runtime-config.md) 与 [u4-l3 内核加载与启动句柄](u4-l3-kernel-load-and-launch.md)。
2. **回到设备侧（Unit 6）**：本讲的 flag（如 `--register-usage-level=10`、`-lineinfo`）最终服务于设备 kernel 的质量与可剖析性，可在 [u6-l1 SM90 FP8 GEMM 内核入口](u6-l1-sm90-fp8-gemm-1d1d-entry.md) 看到这些开关落地的真实 kernel。
3. **工程实践（Unit 10）**：把本讲的 `DG_JIT_DUMP_SASS` / `DG_JIT_WITH_LINEINFO` 与 `cuobjdump`、NCU 串起来做性能剖析，见 [u10-l4 环境变量、调试与性能剖析](u10-l4-env-vars-debug-profiling.md)。
