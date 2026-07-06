# 符号命名契约、制品嵌入与差分模糊测试

## 1. 本讲目标

本讲是「测试、调试与工程化」单元的收束篇，把三件看似无关、实则同源的工程化主题串起来：

1. **符号命名契约**——cuda-oxide 的宏、codegen 后端、lowering、export、运行时加载器五方靠一套带哈希魔数的保留符号前缀互相识别。这套前缀被抽进 `reserved-oxide-symbols` crate 作为唯一真相源。
2. **制品嵌入与锚符号防裁剪**——设备代码被打包成 `.oxart` 段嵌入宿主二进制；对库 crate 而言，链接器会默认丢弃「无人引用」的归档成员，锚符号就是逼链接器把 bundle 留下来的工程手段。
3. **差分模糊测试**——`fuzzer` crate 用一个 FNV-1a 单 `u64`「轨迹」把 CPU 与 GPU 两次执行的中间值折叠成指纹，配合 rustlantis 风格的 MIR 程序生成器，自动捕获 codegen 正确性回归。

学完后你应该能够：

- 说清 `cuda_oxide_*_246e25db_*` 这套命名约定为什么这样设计、被哪几方依赖、破坏它会有什么连锁后果。
- 解释 `.rlib` 归档成员为何会被链接器静默丢弃，以及锚符号 + `SHF_GNU_RETAIN` + 弱别名如何构成三道防线。
- 读懂 `fuzzer` 的 trace API，并说明「同一个 `u64` 相等」如何等价于「CPU/GPU 两次执行的中间值逐位一致」。
- 跑通 `rustlantis-smoke` 差分测试示例，并理解 `PASS / MISMATCH / COMPILE_FAIL / UNSUPPORTED` 四种结果分别意味着什么。

## 2. 前置知识

本讲默认你已经读过：

- **u3-l2 模块加载与内嵌制品**：那里详细讲过 `.oxart` 的 wire 格式（32 字节 header + payload/entry 记录表）、`.oxart` v1/v2 版本协商、运行时 bundle 发现，以及签约模块 loader 的 `unsafe` 边界。本讲**不再重复 wire 格式**，而是从「工程契约」角度补充：命名契约如何防冲突、锚符号如何防裁剪。
- **u6-l4 端到端新增一个 intrinsic**：那里展示了「设备层 → dialect op → importer → lowerer → export」五层协同落地一条 PTX 指令。本讲的命名契约正是这种多层协同能成立的**基础设施**——五层都靠保留符号互相握手。

还需要几个通俗概念：

- **符号（symbol）**：编译产物（目标文件、静态库、可执行文件）里给函数或静态变量起的名字。链接器靠符号名把「别处定义」和「此处引用」配对。
- **名称修饰（name mangling）**：给符号名加前缀/后缀，让它既不和人写的名字撞车，又能编码额外信息（类别、归属）。
- **静态库（archive / `.rlib`）**：一堆目标文件的打包。链接器**按需提取**：只有当某个归档成员定义了别人引用的符号，才把它链进最终二进制；否则整成员被静默丢弃。
- **差分测试（differential testing）**：拿两个实现（这里是 CPU 上的标准 LLVM 后端 vs GPU 上的 cuda-oxide 流水线）跑同一个输入，比对输出，不一致即暴露 bug。模糊测试（fuzzing）负责自动、大量地生成输入。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/reserved-oxide-symbols/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs) | 保留符号前缀的**唯一真相源**：常量层（前缀字符串）、构建器层（给宏用）、谓词/抽取器层（给后端/lowering/export 用），外加一组钉死常量值与互斥性的单元测试。 |
| [crates/oxide-artifacts/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs) | `.oxart` bundle 的序列化/反序列化、编译策略边车，以及把 bundle 包进宿主目标文件、在段首定义锚符号的 `build_host_object_*` 函数族。 |
| [crates/cuda-macros/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs) | 宏侧消费者：用 `RESERVED_ROOT` 拒绝用户越界命名，用 `kernel_symbol` 生成带前缀的内核名，并在 `load_named()` 里 `link_name = anchor` 引用锚符号。 |
| [crates/rustc-codegen-cuda/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs) | 后端侧消费者：把 PTX/cubin 包成 `.oxart` blob、定义段首锚符号、写出宿主目标文件；owner 过滤时写「只有锚、没有 bundle」的占位对象。 |
| [crates/llvm-export/src/export/names.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/llvm-export/src/export/names.rs) | export 侧消费者：用 `device_base_name` 在产出最终 LLVM IR/PTX 前剥掉设备前缀，让内部前缀不泄漏到产物里。 |
| [crates/fuzzer/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/lib.rs) 与 [crates/fuzzer/src/trace.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs) | 差分测试支撑库：`no_std` 的 FNV-1a 单 `u64` 轨迹状态机 + `TraceValue`/`TraceDump` trait，CPU 与 GPU 共用同一份。 |
| [crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs) 与 [generated_case.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs) | rustlantis 风格差分测试的执行外壳：手写 `#[custom_mir]` 函数 + 自动生成的 `generated_case.rs`，CPU 跑一遍、GPU 跑一遍、比 `u64`。 |

## 4. 核心概念与源码讲解

### 4.1 保留符号命名契约（reserved-oxide-symbols）

#### 4.1.1 概念说明

cuda-oxide 的设备代码要穿越一条很长的流水线：宏把 `#[kernel] fn vecadd` 改名 → 后端靠名字把它从代码生成单元里**挑出来**当设备代码 → mir-importer 翻译 → mir-lower 降级 → llvm-export 产出 PTX/LTOIR → 宿主运行时加载。这条链路上的每一方都需要一种可靠方式认出「这是我家的内核」。

最朴素的办法是约定一个前缀字符串，比如 `cuda_oxide_kernel_`。但这有两个工程缺陷：

1. **意外撞名**：用户完全可能写一个叫 `cuda_oxide_kernel_helper` 的普通函数。后端若按前缀匹配，就会把它误当成内核塞进设备流水线。
2. **多处重复**：如果前缀字符串散落在宏、后端、lowering、export 四处各写一遍，改一处漏一处就会出现「宏生成的名字后端认不出」的幽灵 bug。

`reserved-oxide-symbols` crate 用两招同时治这两个病：

- **哈希魔数防撞名**：每个前缀都以 `246e25db` 结尾，它是 `sha256("cuda_oxide_ + rust")` 截断 8 个十六进制字符。文档原话是「没人会手滑写出 `fn cuda_oxide_kernel_246e25db_foo()`」。
- **唯一真相源防重复**：前缀字符串只在这一个 `publish = false` 的 crate 里定义一次，宏侧用「构建器」，消费侧用「谓词/抽取器」，谁都不许自己拼字符串。

它还把这套设计明确定位为**内部 crate**：

> 单一真相源，给宏侧和消费侧的命名契约上锁……常量、构建器、谓词都可能跨 commit 变更，外部消费者应依赖 `cuda-host`/`cuda-device`/`cuda-macros`。
> 见 [crates/reserved-oxide-symbols/src/lib.rs:4-16](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L4-L16)

#### 4.1.2 核心流程

这个 crate 把 API 故意分三层，对应「定义 → 生成 → 识别」三件不同的事：

```text
Layer 1 常量层（raw constants）
  RESERVED_ROOT          = "cuda_oxide_"
  HASH_SUFFIX            = "246e25db"
  KERNEL_PREFIX          = "cuda_oxide_kernel_246e25db_"
  DEVICE_PREFIX          = "cuda_oxide_device_246e25db_"
  DEVICE_EXTERN_PREFIX   = "cuda_oxide_device_extern_246e25db_"
  INSTANTIATE_PREFIX     = "cuda_oxide_instantiate_246e25db_"
  CONSTANT_PREFIX        = "cuda_oxide_const_246e25db_"
  ARTIFACT_ANCHOR_PREFIX = "cuda_oxide_artifact_anchor_246e25db_"
        │
        ▼
Layer 2 构建器层（宏侧调用，把裸名变成带前缀的符号）
  kernel_symbol("vecadd")         → "cuda_oxide_kernel_246e25db_vecadd"
  device_symbol("helper")         → "cuda_oxide_device_246e25db_helper"
  artifact_anchor_symbol(pkg,ver) → "cuda_oxide_artifact_anchor_246e25db_<pkg>_<ver>"
        │
        ▼
Layer 3 谓词/抽取器层（后端/lowering/export 调用，识别并还原裸名）
  is_kernel_symbol(name)   → bool        // 含 KERNEL_PREFIX 子串即真
  kernel_base_name(name)   → Option<&str>// 剥掉前缀，还原 "vecadd"
  display_name(name)       → "vecadd (kernel)"  // 给诊断信息用
```

这里有一个值得专门讲的巧妙设计：**互斥子串保证**（mutual-exclusion guarantee）。

`DEVICE_PREFIX` 和 `DEVICE_EXTERN_PREFIX` 是互斥子串——一个符号不可能同时含两者。这不是巧合，而是哈希后缀的副产物：`cuda_oxide_device_246e25db_` 和 `cuda_oxide_device_extern_246e25db_` 因为都带那段固定哈希，谁也不是谁的前缀。于是消费侧的 `is_device_symbol` **不必**写历史上那种 `contains(DEVICE_PREFIX) && !contains(DEVICE_EXTERN_PREFIX)` 的排除舞步，详见 [crates/reserved-oxide-symbols/src/lib.rs:36-43](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L36-L43)。

另一个关键设计是**子串匹配而非前缀匹配**。MIR 导入会把 `::` 转成 `__`，于是跨 crate 内核会以 FQDN 形式出现，如 `kernel_lib__cuda_oxide_kernel_246e25db_scale`。谓词用 `contains` 而非 `starts_with`，就能同时处理裸形式和 FQDN 形式，省去单独维护一个 `FQDN_*` 常量（见 [crates/llvm-export/src/export/names.rs:12-16](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/llvm-export/src/export/names.rs#L12-L16)）。

#### 4.1.3 源码精读

**Layer 1 常量**——所有前缀共享同一个保留根与哈希后缀：

[crates/reserved-oxide-symbols/src/lib.rs:63-71](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L63-L71) 定义 `RESERVED_ROOT` 与 `HASH_SUFFIX`，注释点明哈希值「具体是多少不重要，重要的是它永远固定、且没人会把它敲进普通函数名」。

[crates/reserved-oxide-symbols/src/lib.rs:78-93](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L78-L93) 定义内核与设备前缀。注意文档说明：内核前缀只用于收集器检测，PTX `.entry` 符号本身仍是干净的裸名（如 `vecadd`）；设备前缀则由 llvm-export 层在产出最终 PTX/LTOIR 前剥掉。

**Layer 2 构建器**——宏侧把裸名拼成完整符号：

[crates/reserved-oxide-symbols/src/lib.rs:146-148](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L146-L148) 的 `kernel_symbol` 只是一行 `format!("{KERNEL_PREFIX}{base}")`，但它的价值在于「拼接逻辑只此一处」。

锚符号构建器更复杂，因为它要把包名、版本（甚至 crate 名、二进制名）消毒成合法链接符号：[crates/reserved-oxide-symbols/src/lib.rs:214-220](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L214-L220) 是遗留版（包名+版本），[crates/reserved-oxide-symbols/src/lib.rs:228-249](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L228-L249) 是 v2 版（追加 crate 名与可选二进制名，并加 `v2_` 前缀和 `_bin_`/`_nonbin` 后缀）。版本号写进符号名，是为了让同一依赖图里两个版本的包各自保留自己的 bundle；消毒函数 `push_symbol_sanitized` 把所有非 `[A-Za-z0-9_]` 字符换成 `_`（见 [crates/reserved-oxide-symbols/src/lib.rs:253-258](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L253-L258)）。

**Layer 3 谓词/抽取器**——消费侧识别并还原：

[crates/reserved-oxide-symbols/src/lib.rs:272-274](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L272-L274) 的 `is_kernel_symbol` 用 `name.contains(KERNEL_PREFIX)`；[crates/reserved-oxide-symbols/src/lib.rs:287-289](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L287-L289) 的 `is_device_symbol` 注释明确指出：因为互斥性，此处无需为 extern 特判。

[crates/reserved-oxide-symbols/src/lib.rs:344-347](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L344-L347) 的 `kernel_base_name` 用 `find` 定位前缀位置再截取——这让它能跳过 FQDN 限定符。

[crates/reserved-oxide-symbols/src/lib.rs:401-413](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L401-L413) 的 `display_name` 把符号翻成 `vecadd (kernel)` 这类人话标签，专给诊断信息用；它特意先判 device-extern 再判 device，万一哪天互斥性被改坏也能优先报对外种类。

**真实消费侧**——宏用 `RESERVED_ROOT` 在源码层拒绝用户越界命名：

[crates/cuda-macros/src/lib.rs:182-195](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L182-L195) 的 `reject_reserved_name`：用户若写 `fn cuda_oxide_kernel_evil`，宏在展开期就报编译错误「这个命名空间保留给 cuda-oxide 内部符号修饰」。这是契约的第一道闸门——让撞名根本进不了流水线。

**真实消费侧**——export 层剥前缀，让内部约定不泄漏到产物：

[crates/llvm-export/src/export/names.rs:31-38](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/llvm-export/src/export/names.rs#L31-L38) 的 `strip_device_prefix` 在产出最终 LLVM IR/PTX 前调用 `device_base_name` 剥掉前缀；device-extern 声明则保持原名（它们要靠原始名和外部符号配对）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「哈希魔数 + 子串匹配」如何把合法用户名挡在契约外。

**操作步骤**：

1. 阅读 [crates/reserved-oxide-symbols/src/lib.rs:577-594](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L577-L594) 的 `user_names_with_old_prefix_are_not_matched` 测试。它列出了五种「邪恶名」：`cuda_oxide_kernel_evil`、`cuda_oxide_device_evil` 等——这些是**没有哈希后缀**的旧式/意外形式。
2. 在 `crates/reserved-oxide-symbols` 目录下跑测试：
   ```bash
   cargo test -p reserved-oxide-symbols
   ```
3. 阅读钉死常量值的测试 [crates/reserved-oxide-symbols/src/lib.rs:460-468](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L460-L468)，注释写明「改这个常量是对所有已构建 cuda-oxide 制品的破坏性变更，必须故意为之」。

**需要观察的现象**：所有谓词对「邪恶名」返回 `false`，`display_name` 返回 `None`——它们被当成无关用户代码，不会被任何消费方误识别。

**预期结果**：测试全绿。如果你想制造失败，临时把 `HASH_SUFFIX` 改成空串再跑，会看到 `user_names_with_old_prefix_are_not_matched` 直接挂掉——这直观演示了哈希后缀是契约的安全锁。

> 待本地验证：在没有 CUDA/LLVM 环境的机器上，`cargo test -p reserved-oxide-symbols` 这个 `no_std` crate 的单元测试仍可独立运行，因为它不依赖任何 GPU 工具链。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_device_symbol` 不需要写 `&& !is_device_extern_symbol` 的排除逻辑？

**参考答案**：因为哈希后缀让 `DEVICE_PREFIX`（`..._device_246e25db_`）和 `DEVICE_EXTERN_PREFIX`（`..._device_extern_246e25db_`）成为互斥子串——谁也不是对方的前缀。所以一个 extern 符号不可能 `contains(DEVICE_PREFIX)` 为真，排除逻辑自然不必要。这条性质被 [crates/reserved-oxide-symbols/src/lib.rs:493-499](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L493-L499) 的测试钉死。

**练习 2**：若有人把宏里的 `kernel_symbol` 换成自己拼的 `format!("cuda_oxide_kernel_{base}")`（漏掉哈希后缀），会引发什么连锁问题？

**参考答案**：宏生成的内核名不再含 `KERNEL_PREFIX` 子串，于是后端收集器（`is_kernel_symbol`）认不出它，该函数不会被收进设备可达集，进而 PTX 里缺这个 `.entry`；同时 export 层的 `strip_device_prefix`/`kernel_base_name` 也匹配不到，PTX 里会出现带错误前缀的符号；最终运行时 `load()` 找不到内核，报 `ModuleNotFound` 或启动失败。这就是「唯一真相源」要防的幽灵 bug。

### 4.2 .oxart 制品嵌入与锚符号防裁剪（oxide-artifacts）

#### 4.2.1 概念说明

`.oxart` 是一段自描述的设备代码 bundle（wire 格式见 u3-l2）。后端要把它**装进宿主可执行文件**，运行时再由 `cuda-host` 自省当前可执行文件、把 bundle 找回来加载。这一节不讲 wire 格式，专讲「装进去」这一步的两个工程难点：

1. **怎么把一段裸字节变成链接器认识的目标文件？**——`oxide-artifacts` 的 `build_host_object_for_target` 把 `.oxart` 数据包成一个只含一个数据段的可重定位目标文件。
2. **库 crate 的 bundle 为什么会被链接器静默丢弃，怎么救？**——这就是**锚符号（anchor symbol）**要解决的核心问题。

问题 2 是真实踩过的坑（代码注释里点名 issue #72）。理解它需要先知道链接器处理静态库（`.rlib`/`.a`）的规则：

> 链接器**只有**在某个归档成员**定义**了一个被别人引用的符号时，才会把这个成员提取出来链进最终二进制；否则整成员被静默跳过。

一个「只装了 `.oxart` 数据、不定义任何符号」的目标文件，对链接器而言就是「没人需要我」，于是被丢弃。结果：lib crate 的 bundle 永远到不了最终二进制，运行时 `load()` 报 `ModuleNotFound`。

解法是**锚符号握手**：后端在 `.oxart` 段首定义一个全局锚符号；`#[cuda_module]` 宏在生成的 `load_named()` 里**引用**这个符号。于是只要有人调 `load()`，就产生一个未定义引用，逼链接器把装着 bundle 的归档成员提取出来。代码注释把来龙去脉讲得很清楚，见 [crates/oxide-artifacts/src/lib.rs:627-639](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L627-L639)。

#### 4.2.2 核心流程

完整的「装进去 → 留下来」链路：

```text
后端为某 crate 产出 PTX/cubin
        │
        ▼
build_artifact_blob(spec)            序列化成 .oxart 字节流（u3-l2 讲过格式）
        │
        ▼
build_host_object_for_target(blob, target, anchor_symbol)
        │  ┌─ 新建一个可重定位目标文件（ELF）
        │  ├─ 加一个 .oxart 数据段，塞入 blob
        │  ├─ 段首定义全局锚符号（SymbolScope::Linkage：能被别处引用，但不进动态符号表）
        │  └─ 段打上 SHF_ALLOC | SHF_GNU_RETAIN（防 --gc-sections 二次裁剪）
        ▼
对 binary crate：目标文件直接交给链接器（段必然存活）
对 library crate：目标文件变成 .rlib 归档的一个成员
        │
        ▼
#[cuda_module] 宏在 load_named() 里：
        unsafe extern "C" { #[link_name = anchor] static CUDA_OXIDE_BUNDLE_ANCHOR: u8; }
        let _artifact_anchor: *const u8 = &CUDA_OXIDE_BUNDLE_ANCHOR;
        │  ← 这一引用制造「未定义符号」，逼链接器提取归档成员
        ▼
最终二进制里：.oxart 段存活，运行时 cuda-host 自省可执行文件、找回 bundle
```

这里有**三道防线**层层兜底，每道都对应真实失败模式：

| 防线 | 机制 | 防的是哪种失败 |
| --- | --- | --- |
| 第一道：锚符号握手 | 段首定义全局符号 + 宏侧 `link_name` 引用 | 归档成员因「无人引用」被链接器整成员丢弃（issue #72） |
| 第二道：`SHF_GNU_RETAIN` | 段标志位 `SHF_ALLOC \| SHF_GNU_RETAIN` | 成员**已**链入后，被 `--gc-sections` 当死段二次裁剪 |
| 第三道：弱遗留别名 | v2 目标锚之外再加一个 weak 的包级遗留锚 | 新宏（owner 感知）与旧宏（只认遗留锚）混用时的链接兼容 |

第三道防线值得展开。新版的 owner 过滤机制要求锚符号带上 crate 名甚至二进制名（v2 锚），但**老版本宏**展开出的代码只引用遗留的包级锚。如果只定义 v2 锚，旧宏的链接就断了。于是 `build_host_object_for_target_with_legacy_anchor` 同时定义一个强 v2 锚和一个 **weak** 遗留别名：强符号满足新宏的精确引用，weak 别名满足旧宏的遗留引用且不会因「一个包多个目标」而触发重复符号错误。见 [crates/oxide-artifacts/src/lib.rs:661-682](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L661-L682)。

还有一个边界情况：当一个 crate 被 owner 过滤**故意抑制**了设备制品（本不该出 bundle），但旧宏仍会引用它的遗留锚。此时后端写一个「只有锚、没有 `.oxart` 段」的占位对象（`build_host_anchor_object_for_target`），既让旧链接过得去，又让运行时制品发现正确地看到「这个 crate 没有设备 bundle」。见 [crates/oxide-artifacts/src/lib.rs:684-709](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L684-L709)。

#### 4.2.3 源码精读

**段名与魔数**：

[crates/oxide-artifacts/src/lib.rs:14-23](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L14-L23) 定义 `.oxart`（bundle 段）、`.oxlink`（仅锚占位段）、`OXIDEART` 魔数、版本号 v2/legacy v1，以及定长记录大小（header 32 字节、payload/entry 各 24 字节）。`.oxart` 段名特意 ≤ 8 字符（被 [crates/oxide-artifacts/src/lib.rs:1097-1100](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L1097-L1100) 钉死），以兼容某些目标文件格式对段名长度的限制。

**把 bundle 包进目标文件并定义锚**：

[crates/oxide-artifacts/src/lib.rs:640-659](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L640-L659) 的 `build_host_object_for_target` 是核心入口：空数据直接报错；给了锚就委托 `build_host_object_with_section` 在 `.oxart` 段首定义该符号，否则产出一个无符号对象（测试与非 rlib 嵌入用）。

[crates/oxide-artifacts/src/lib.rs:711-754](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L711-L754) 的 `build_host_object_with_section` 干三件事：

- 段打上 `SHF_ALLOC | SHF_GNU_RETAIN`（[crates/oxide-artifacts/src/lib.rs:730-732](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L730-L732)），后者是第二道防线。
- 每个锚符号用 `SymbolScope::Linkage`（[crates/oxide-artifacts/src/lib.rs:734-749](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L734-L749)）：全局绑定让它能满足别处的未定义引用（从而触发归档提取），但 `Linkage` 可见性让它隐藏、不泄漏进最终二进制的动态符号表。`weak` 形参控制是否弱绑定（遗留别名用）。
- 段对齐到 8 字节。

**端到端验证弱别名真能从静态库提取 bundle**：

[crates/oxide-artifacts/src/lib.rs:1246-1310](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L1246-L1310) 的 `weak_legacy_alias_extracts_artifact_from_static_archive` 是本节最有教学价值的测试：它真的把 bundle 对象用 `ar crs` 打成 `libartifact.a`，写一个引用 `legacy_anchor` 的 C `main`，用 `cc` 链接成可执行文件，再用 `read_artifact_bundles_from_object_bytes` 自省可执行文件，断言 bundle 确实被提取进来了。这条测试把「锚符号 → 归档提取 → bundle 存活」整条链路用真实的 `ar`/`cc` 跑通，是防 dead-strip 回归的护身符。

**后端真实调用**：

[crates/rustc-codegen-cuda/src/lib.rs:834-866](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L834-L866) 是后端把 PTX 包成 blob、定义锚、写目标文件的现场。注释（[crates/rustc-codegen-cuda/src/lib.rs:835-845](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L835-L845)）点名 issue #72，说明「没有锚，lib crate 的 bundle 会被 dead-strip，运行时 `load()` 报 ModuleNotFound」。owner 感知构建用 v2 锚 + 弱遗留别名，普通构建只用遗留锚。

**宏侧真实引用锚符号**：

[crates/cuda-macros/src/lib.rs:1291-1314](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1291-L1314) 展示宏如何「制造未定义引用」：根据 owner 选择算出锚名（v2 或遗留），然后用 `unsafe extern "C" { #[link_name = anchor_name] static CUDA_OXIDE_BUNDLE_ANCHOR: u8 }` 声明一个外部符号并取其地址。这一行 `let _artifact_anchor: *const u8 = ...` 就是逼链接器提取归档成员的那根线。

#### 4.2.4 代码实践

**实践目标**：用真实 `ar`/`cc` 亲眼看见「弱锚符号把 bundle 从静态库里拉出来」。

**操作步骤**：

1. 阅读 [crates/oxide-artifacts/src/lib.rs:1246-1310](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L1246-L1310) 的 `weak_legacy_alias_extracts_artifact_from_static_archive` 测试，理解它做的事。
2. 在 Linux x86_64 上跑这条测试：
   ```bash
   cargo test -p oxide-artifacts --features object-read,object-write weak_legacy_alias_extracts_artifact_from_static_archive
   ```
3. 同时跑它的「反例」对照：[crates/oxide-artifacts/src/lib.rs:1159-1169](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L1159-L1169) `host_object_without_anchor_has_no_symbols`，确认不给锚时目标文件零符号。

**需要观察的现象**：带弱遗留别名的对象打进 `libartifact.a` 后，C 程序里对 `legacy_anchor` 的引用能成功解析（`cc` 链接成功），最终可执行文件里能自省出名为 `demo` 的 bundle；不给锚的对象则零符号。

**预期结果**：两条测试均通过。这直接演示了「锚符号是 bundle 不被链接器丢弃的唯一原因」。

> 待本地验证：该测试要求 `target_os = "linux" && target_arch = "x86_64"` 且系统装了 `ar` 与 `cc`，否则被 `#[cfg]` 跳过。

#### 4.2.5 小练习与答案

**练习 1**：为什么锚符号要用 `SymbolScope::Linkage` 而不是默认的全局导出（`SymbolScope::Dynamic`）？

**参考答案**：`Linkage` 让符号保持全局绑定（能满足别处未定义引用、触发归档提取），但**隐藏**它，不进最终二进制的动态符号表。锚只是 cuda-oxide 内部的链接握手手段，没有理由让它出现在 `.dynsym` 里污染 ABI 表面、或被外部工具误用。见 [crates/oxide-artifacts/src/lib.rs:734-749](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L734-L749) 注释。

**练习 2**：`SHF_GNU_RETAIN` 与锚符号握手各防的是哪一层裁剪？

**参考答案**：锚符号握手防的是**链接器选成员阶段**的丢弃——归档成员因「无人引用」根本不被提取；`SHF_GNU_RETAIN` 防的是**段级死代码消除**（`--gc-sections`）——成员已被链入，但段因「看似无人用」被当作死段回收。两者作用于链接的不同阶段，缺一不可。

### 4.3 fuzzer 差分测试支撑（trace API）

#### 4.3.1 概念说明

cuda-oxide 是一条很长且容易出微妙错误的 codegen 流水线（MIR 翻译 → mem2reg → 循环展开 → lowering → NVVM legalize → 导出）。一条 lowering 回归可能只让某个中间整型算错一个 bit，最终结果「看起来差不多」却已经错了。怎么自动、大量地抓这种 bug？

经典答案是**差分模糊测试**：自动生成海量随机程序，分别用两个实现跑，比对结果。cuda-oxide 的两个实现天然存在——CPU 走标准 LLVM 后端（视为正确 oracle），GPU 走 cuda-oxide 流水线（被测对象）。

但「比对结果」有个工程难题：生成的随机程序返回类型千变万化（元组、各种整型），逐字段比很啰嗦。`fuzzer` crate 的解法极其优雅：**把所有中间值折叠进一个 `u64` 指纹**，只比这一个数。

`fuzzer` crate 的定位见其根文档 [crates/fuzzer/src/lib.rs:6-24](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/lib.rs#L6-L24)：「差分 codegen 模糊测试支撑 crate，承载 rustlantis 风格差分测试器共享的可复用件」——具体就是一个单 `u64` 轨迹状态机加 `TraceValue`/`TraceDump` 两个 trait。它刻意 `no_std`，既不依赖 CUDA 也不依赖 std，这样 CPU 和 GPU 两侧能用同一份代码折叠轨迹。

#### 4.3.2 核心流程

轨迹状态机用 **FNV-1a 64 位**哈希。每遇到一个「值得记录的中间值」，就逐字节异或再乘以一个质数，混进累积哈希。其递推关系为：

\[
h_0 = \text{FNV\_OFFSET} = \texttt{0xcbf29ce484222325}
\]

\[
h_i = (h_{i-1} \oplus b_i) \cdot \text{FNV\_PRIME} \pmod{2^{64}}, \quad \text{FNV\_PRIME} = \texttt{0x100000001b3}
\]

其中 \(b_i\) 是被折叠值的第 \(i\) 个字节（小端序）。多字节值按字节逐个喂入；元组按成员顺序逐个喂入。

为什么这等价于「中间值逐位一致」？FNV-1a 是**确定性**的——同样的字节序列必然产生同样的 `u64`。所以：

\[
\text{CPU 轨迹} = \text{GPU 轨迹} \;\Longleftrightarrow\; \text{两次执行喂进的所有中间值字节序列完全相同}
\]

只要任一中间值差一个 bit，最终 `u64` 几乎必然不同（哈希的雪崩效应）。于是「比一个 `u64`」就成了「比整条执行轨迹」的高保真代理。

整个差分闭环：

```text
同一份生成的程序（含若干 dump_var 调用）
        │
   ┌────┴─────────────────────────┐
   ▼                              ▼
CPU 路径（标准 LLVM 后端）      GPU 路径（cuda-oxide 流水线）
   │                              │
   { trace_reset(); … dump_var … }← 两边都调同一套 fuzzer trace API
   │                              │
   trace_finish() → cpu_hash      trace_finish() → gpu_hash
   │                              │
   └──────────┬───────────────────┘
              ▼
       cpu_hash == gpu_hash ?
         ├─ 是 → PASS
         └─ 否 → MISMATCH（高度疑似 codegen 正确性 bug）
```

一个关键细节：轨迹初值从 `FNV_OFFSET` 开始，且 trace API 注释明确说「状态从零开始，因为 cuda-oxide 目前只支持零初始化的设备静态量」（见 [crates/fuzzer/src/trace.rs:11-14](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L11-L14)）。也就是说 `RL_TRACE` 这个 `static mut` 在 GPU 上会被 cuda-oxide 当设备静态量处理、初始化为零，每次运行前必须 `trace_reset` 把它置回 FNV 基。

#### 4.3.3 源码精读

**轨迹状态与 FNV 常量**：

[crates/fuzzer/src/trace.rs:22-25](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L22-L25) 定义 FNV 偏移基、FNV 质数与全局 `static mut RL_TRACE: u64 = 0`。这个 `static mut` 既是 GPU 端的设备静态量，也顺便阻止优化器把轨迹状态常量折叠掉。

**复位/读出/逐字节混合**：

[crates/fuzzer/src/trace.rs:28-33](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L28-L33) `trace_reset` 把状态写回 FNV 基；[crates/fuzzer/src/trace.rs:36-39](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L36-L39) `trace_finish` 读出当前状态；[crates/fuzzer/src/trace.rs:41-46](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L41-L46) `trace_write_byte` 就是 FNV-1a 一步：异或字节、wrapping 乘质数。

**多字节拆分**：

[crates/fuzzer/src/trace.rs:69-75](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L69-L75) 的 `trace_write_u32` 展示了小端逐字节喂入的模式（u64/usize 等同理）。注意 [crates/fuzzer/src/trace.rs:82-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L82-L86) `trace_write_u64` 是拆成两个 u32 再喂——递归复用。

**两个 trait 把任意标量/元组统一成「可折叠」**：

[crates/fuzzer/src/trace.rs:124-157](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L124-L157) 的 `TraceValue` 用宏为 14 种标量（`bool`/`i8`…`u128`/`isize`/`usize`/`char`）统一实现；[crates/fuzzer/src/trace.rs:159-218](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L159-L218) 的 `TraceDump` 为 `()` 与 arity 1–5 的元组实现，逐成员调 `TraceValue::trace_write`。注释点明「arity 上限 5 匹配 rustlantis 当前 prune 掉 unit 后的最大参数束」。

[crates/fuzzer/src/trace.rs:220-228](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L220-L228) 的 `dump_var<T: TraceDump>` 是生成代码调用的统一入口——一个调用点处理所有参数形状。

**`#[inline]` 的必要性**：

[crates/fuzzer/src/trace.rs:16-20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L16-L20) 注释解释：所有 trace 函数都标 `#[inline]`，是为了让它们的 MIR 被编进 `fuzzer` 的 rlib、从而在 smoke 示例为设备编译时对 cuda-oxide 的 MIR 收集器**可达**——否则收集器看不到这些函数体，GPU 端就无从执行 trace 逻辑。

#### 4.3.4 代码实践

**实践目标**：手算一个小轨迹，建立「值→字节→u64」的直觉。

**操作步骤**：

1. 阅读 [crates/fuzzer/src/trace.rs:41-46](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L41-L46) 与 [crates/fuzzer/src/trace.rs:69-75](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L69-L75)，理解 FNV-1a 单步与 u32 拆分。
2. 写一个小程序（示例代码，非项目原有）调用 trace API，折叠单个 `u32` 值 `0x01`：
   ```rust
   // 示例代码：仅演示手算，不是项目原有文件
   use fuzzer::{trace_reset, trace_finish, dump_var};
   fn main() {
       trace_reset();
       dump_var((0x01u32,));
       println!("0x{:016x}", trace_finish());
   }
   ```
3. 手算预期：`trace_reset` 后 `h = FNV_OFFSET`；`0x01u32` 小端拆成字节 `0x01, 0x00, 0x00, 0x00`，连做 4 步 FNV-1a。
4. 运行（在 `rustlantis-smoke` 同级新示例里，或临时改 `rustlantis-smoke` 的 stage1）对比手算与程序输出。

**需要观察的现象**：手算的 4 步 FNV-1a 结果应与程序输出逐位一致。

**预期结果**：建立「任意值都被规约成确定性的 u64」的信心——这正是差分测试成立的基础。

> 待本地验证：手算大数乘法易错，建议用 Python 做对照（`h = (h ^ b) * 0x100000001b3 % 2**64`），再与 Rust 输出比。

#### 4.3.5 小练习与答案

**练习 1**：为什么 trace API 刻意 `no_std`？

**参考答案**：因为它要被同一份源码在两条路径上编译执行——CPU 路径（标准宿主，有 std）与 GPU 路径（设备代码，经 cuda-oxide 流水线，无 std）。`no_std` 让 trace 代码不依赖 std，从而能在设备端编译通过，保证 CPU 与 GPU 跑的是**字面相同**的折叠逻辑（否则两边逻辑分叉，差分测试就失去了「同一 oracle」的前提）。见 [crates/fuzzer/src/lib.rs:6-24](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/lib.rs#L6-L24)。

**练习 2**：如果两个中间值在不同执行中顺序不同但集合相同，轨迹会相等吗？这说明什么？

**参考答案**：不会相等。FNV-1a 是**有序**哈希——字节序列顺序不同结果就不同。这说明差分测试不只校验「出现了哪些值」，还校验「值出现的顺序」，即校验的是控制流与数据流的完整轨迹，而非仅仅是最终结果集合。

### 4.4 rustlantis 风格生成器与差分闭环

#### 4.4.1 概念说明

4.3 节解决了「怎么比」，本节解决「拿什么程序来比」。手写测试覆盖面有限，cuda-oxide 借用了 **rustlantis**——一个能随机生成合法 `#[custom_mir]` Rust 程序的差分测试器（思路源自 Rust 编译器团队的同名项目）。生成出的程序满是奇怪的字节运算、`transmute`、移位、位翻转——正是容易让 lowering 出错的形状。

整个生成闭环由 `crates/fuzzer/tools/` 下两个 Python 脚本驱动：

- `mir_generator.py`：把一个 rustlantis seed 适配成 cuda-oxide smoke 用例（关键动作是把 rustlantis 自己的 `dump_var` 调用改写成 `fuzzer::dump_var`）。
- `run_seed.py`：生成一个 seed、注入 `rustlantis-smoke` 的 `generated_case.rs`、跑 CPU 与 GPU、记录结果。

注意 `fuzzer` 的 `Cargo.toml` 特意 `exclude = ["rustlantis/"]`（见 [crates/fuzzer/Cargo.toml:11-13](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/Cargo.toml#L11-L13)）——vendored 的 rustlantis 是**外部程序生成器**，通过 `cargo build` 被 Python 调用，而非作为 Rust 依赖被消费。

#### 4.4.2 核心流程

```text
python3 crates/fuzzer/tools/run_seed.py --seed N
        │
        ▼
rustlantis 按 seed 生成一个 custom-MIR 函数（含若干 dump_var 调用）
        │
        ▼
mir_generator.py 适配：
   - 把 rustlantis 的 dump_var(...) 改写成 fuzzer::dump_var(...)
   - 包成 compute_rustlantis_trace() { trace_reset(); fn(...); trace_finish() }
        │
        ▼
改写 crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs
（main.rs 与 trace API 保持不动，是稳定的外壳）
        │
        ▼
rustlantis-smoke 在 CPU 与 GPU 上各跑一次同一个 generated_case
        │
        ▼
比较两个 u64 轨迹 → PASS / MISMATCH / COMPILE_FAIL / UNSUPPORTED
        │
        ▼
写日志到 crates/fuzzer/artifacts/seed-<N>-<status>.log
汇总到 crates/fuzzer/artifacts/summary.jsonl
```

四种结果状态的含义（见 [crates/fuzzer/README.md](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/README.md) 的 *Result statuses* 段）：

| 状态 | 含义 | 优先级 |
| --- | --- | --- |
| `PASS` | 适配器产出了用例、CPU/GPU 都跑完、两个 u64 相等 | — |
| `MISMATCH` | CPU/GPU 都跑完但 u64 不等 → **高度疑似 codegen 正确性 bug** | 最高 |
| `COMPILE_FAIL [backend]` | 适配成功，但 cuda-oxide 编译或运行时失败（记录后端原因 + 用例快照） | 高 |
| `UNSUPPORTED [adapter]` | rustlantis 生成了程序，但 Python 适配器拒绝转成 smoke 用例（如 dump 了 `u128`，trace API 还不支持） | 信息性 |

#### 4.4.3 源码精读

**执行外壳：手写 stage1 + 自动 stage2**：

[crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs:51-78](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs#L51-L78) 是手写的 `#[custom_mir]` 函数 `fn0`：一串算术 + 异或 + 移位 + 位旋转，作为 stage1「外壳自检」——证明差分骨架本身能跑通。

[crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs:80-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs#L80-L86) 的 `compute_stage1_trace` 是标准的「reset → 执行 → finish」三段式，把 `fn0` 的返回元组 `dump_var` 进轨迹。

**GPU 内核与宿主比对**：

[crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs:94-103](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs#L94-L103) 的 `#[kernel] rl_smoke` 用两个 `DisjointSlice<u64>` 分别接 stage1/stage2 的轨迹哈希——注意它**在 GPU 上调用** `compute_stage1_trace` 与 `generated_case::compute_rustlantis_trace`，这正是差分测试要的「同一份代码在 GPU 上重跑」。

[crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs:109-156](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs#L109-L156) 的 `main` 是比对现场：CPU 上算 `cpu_stage1`/`cpu_stage2`，GPU 上启动内核（[crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs:134-143](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/main.rs#L134-L143) 用 `cuda_launch!` 包在 `unsafe` 块里——raw `LaunchConfig` 启动需调用方自证，见 u1-l4/u2-l4），回读后逐个比 `u64`，相等打印 `PASS`，不等打印 `MISMATCH — INVESTIGATE` 并 `exit(1)`。

**自动生成的 stage2 用例**：

[crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs:6-8](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs#L6-L8) 的头注释说明它是 `mir_generator.py` 自动生成、rustlantis seed 19、并把 dump 调用适配到 fuzzer 的全局轨迹。函数体 [crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs:15-48](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs#L15-L48) 充满 `!`、`as`、`>>`、`transmute` 等「易错形状」，其中 [crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs:41-42](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs#L41-L42) 的 `dump_var(Move(__rl_dump0))` 就是经适配后的轨迹采集点。

**生成器驱动**：

[crates/fuzzer/tools/run_seed.py:21-27](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/tools/run_seed.py#L21-L27) 定位了关键路径：`MIR_GENERATOR` 指向 `mir_generator.py`，`GENERATED_CASE` 指向 smoke 示例里被改写的那个文件——脚本只重写 `generated_case.rs`，`main.rs` 始终是稳定的 CPU/GPU 外壳。这把「易变的生成用例」与「稳定的执行骨架」干净分离。

#### 4.4.4 代码实践

**实践目标**：跑通差分闭环，亲眼看到一次 CPU/GPU 轨迹比对。

**操作步骤**：

1. 跑当前 checked-in 用例（seed 19）：
   ```bash
   cargo oxide run rustlantis-smoke
   ```
2. 阅读输出：应看到 `Stage 1b CPU hash` 与 `Stage 1b GPU hash` 相等、`Stage 2a CPU hash` 与 `Stage 2a GPU hash` 相等，结尾 `PASS: CPU/GPU traces match`。
3. 再用 Python 驱动生成并跑一个新 seed（无需 GPU 也能看适配阶段，但完整比对需 GPU）：
   ```bash
   python3 crates/fuzzer/tools/run_seed.py --seed 192
   ```
4. 查看产物：
   ```bash
   ls crates/fuzzer/artifacts/
   cat crates/fuzzer/artifacts/summary.jsonl
   ```
5. 跑一个小批量，观察不同 seed 的状态分布：
   ```bash
   python3 crates/fuzzer/tools/run_seed.py --start 0 --count 20 --keep-going
   ```

**需要观察的现象**：seed 19 应为 `PASS`；批量跑里会出现 `UNSUPPORTED [adapter]`（如某 seed dump 了 `u128`，trace API 暂不支持）与 `COMPILE_FAIL [backend]`（如某 seed 用了 cuda-oxide 尚未实现的 `RigidTy(Char)`），偶尔可能出现 `MISMATCH`。

**预期结果**：`PASS` 时 CPU/GPU 两个 `u64` 逐位相等；任何 `MISMATCH` 都是一条值得追查的 codegen 正确性线索，其日志含 seed、status、reason、命令、完整输出与 `generated_case.rs` 快照，便于复现。

> 待本地验证：完整差分比对需要真实 GPU 与 cuda-oxide 工具链；纯 CPU 环境只能走到适配阶段（`UNSUPPORTED`/适配器报错），无法产生 `PASS`/`MISMATCH`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `run_seed.py` 只改写 `generated_case.rs`，而不动 `main.rs`？

**参考答案**：因为执行骨架（CPU 算哈希、GPU 启内核、回读比对）对所有 seed 都一样，只有「被测程序体」随 seed 变。把易变的生成代码隔离进单独文件、保持外壳稳定，既让每个 seed 的 diff 最小，也让失败复现时只需看一个文件。见 [crates/fuzzer/tools/run_seed.py:21-27](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/tools/run_seed.py#L21-L27) 与 [crates/fuzzer/README.md](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/README.md)。

**练习 2**：`MISMATCH` 与 `COMPILE_FAIL [backend]` 哪个更值得优先调查？为什么？

**参考答案**：`MISMATCH`。因为它意味着 cuda-oxide **成功编译并运行**了程序，却算出了与 CPU oracle 不同的结果——这是最危险的「静默错误」：用户拿到的结果看似正常实则错误。`COMPILE_FAIL` 至少是显式失败（用户会立刻知道有问题），危害低于静默错算。这也是 README 把 `MISMATCH` 标为「最高优先级」的原因。

## 5. 综合实践

把三块知识串成一个调查任务。假设你收到一条 `rustlantis-smoke` 的 `MISMATCH` 报告（某 seed 的 CPU 与 GPU 轨迹 `u64` 不等），按以下流程定位它：

1. **复现并固定输入**。从 `crates/fuzzer/artifacts/seed-<N>-MISMATCH.log` 取出 seed 与 `generated_case.rs` 快照，用 `python3 crates/fuzzer/tools/run_seed.py --seed <N>` 复现，确认仍 `MISMATCH`。这一步依赖 4.4 的差分闭环。
2. **缩小轨迹分歧点**。打开 `generated_case.rs`，定位所有 `dump_var(...)` 调用（如 [crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs:41-42](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/rustlantis-smoke/src/generated_case.rs#L41-L42)）。临时注释掉靠后的 dump 点，只留第一个，看 `MISMATCH` 是否消失——二分找出第一个开始错算的 dump 点，从而锁定出错的那段运算（如某次 `>>`、`transmute` 或异或）。
3. **判断属于哪一层**。若出错的运算涉及整型符号性（`>>`/`div`/`cmp`），优先怀疑 mir-lower 的 signless 恢复（见 u6-l3）；若涉及浮点，怀疑 FMA 收缩策略（见 u4-l5）；若涉及控制流，怀疑 mem2reg 或循环展开（见 u6-l5）。
4. **写一段说明**（对应规格里的实践任务）：用你自己的话回答两个问题——
   - **命名契约被破坏会引发什么连锁问题**：以 4.1.5 练习 2 为引子，说明若 `KERNEL_PREFIX` 的哈希后缀漏掉，宏生成的名字会被收集器（`is_kernel_symbol`）、export 层（`strip_device_prefix`）、运行时加载器三方同时失配，最终表现为 `ModuleNotFound`。
   - **差分模糊测试如何捕获一个 lowering 回归**：以本调查任务为例，说明 FNV-1a 轨迹把「某次 lowering 把 `sdiv` 错降级成 `udiv`」这种单 bit 错误放大成整个 `u64` 不等，从而被 `MISMATCH` 捕获，而手写单元测试很难覆盖到这种随机生成的「易错形状」。
5. （可选）**修复后回归**：修好 lowering 后重跑该 seed 确认 `PASS`，再跑 `--count 20` 确认没引入新 `MISMATCH`。

> 待本地验证：本综合实践需要真实 GPU 与 cuda-oxide 工具链才能复现 `MISMATCH`；纯阅读型替代方案是——找一个已知 `COMPILE_FAIL [backend]` 的 seed，按上述 1–3 步定位它是 mir-importer 还是 mir-lower 抛的错（错误信息里的 `Unsupported construct:` 前缀指向 mir-importer，见 [crates/fuzzer/tools/run_seed.py](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/tools/run_seed.py) 的 `reason_from_output`）。

## 6. 本讲小结

- **保留符号命名契约**：`reserved-oxide-symbols` 是 `cuda_oxide_*_246e25db_*` 这套前缀的唯一真相源，用 `sha256` 截断哈希防意外撞名、用「常量/构建器/谓词」三层 API 防多处重复，并用 `RESERVED_ROOT` 在宏展开期拒绝用户越界命名。互斥子串保证让 device 与 device-extern 谓词免写排除逻辑，子串匹配让裸名与 FQDN 名统一处理。
- **制品嵌入与锚符号防裁剪**：`.oxart` bundle 被包进宿主目标文件嵌入二进制；对库 crate，链接器默认丢弃无人引用的归档成员（issue #72）。三道防线兜底：段首全局锚符号 + 宏侧 `link_name` 引用触发归档提取、`SHF_GNU_RETAIN` 防 `--gc-sections` 二次裁剪、弱遗留别名保新宏与旧宏混用时的链接兼容。
- **fuzzer 差分测试支撑**：一个 `no_std` 的 FNV-1a 单 `u64` 轨迹把 CPU 与 GPU 两次执行的中间值折叠成指纹，「两个 u64 相等」等价于「整条执行轨迹逐位一致」。`TraceValue`/`TraceDump` 把任意标量与 arity≤5 元组统一成可折叠，`dump_var` 是生成代码的统一入口。
- **rustlantis 风格生成器**：vendored rustlantis 随机生成 `#[custom_mir]` 程序，`mir_generator.py`/`run_seed.py` 适配并注入 `rustlantis-smoke` 的 `generated_case.rs`，外壳 `main.rs` 跑 CPU 与 GPU 比对，产出 `PASS`/`MISMATCH`/`COMPILE_FAIL`/`UNSUPPORTED` 四态。
- **三者的共同主线**：它们都是「让一条长而脆弱的 codegen 流水线可信赖」的工程基础设施——命名契约让多方可靠握手、锚符号让制品可靠存活、差分模糊让正确性回归被自动捕获。

## 7. 下一步学习建议

- **想加深运行时加载侧的理解**：回到 u3-l2，结合本讲的锚符号握手，对照阅读 `crates/cuda-host/src/embedded.rs` 与 `crates/cuda-core/src/embedded.rs`，追踪「自省可执行文件 → 找 `.oxart` 段 → 解析 bundle → 加载」的运行时后半段。
- **想亲手扩展差分测试覆盖面**：当前 trace API 不支持 `u128`/`char` 等（见 [crates/fuzzer/src/trace.rs:142-157](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/fuzzer/src/trace.rs#L142-L157) 与 README 里 seed 0 的 `UNSUPPORTED` 例子）。可尝试为 `u128` 实现 `TraceValue`（注意 cuda-oxide 设备端对 128 位整型的支持情况），让更多 seed 从 `UNSUPPORTED` 变成可执行，从而扩大模糊覆盖。
- **想理解命名契约在新 intrinsic 落地中的角色**：结合 u6-l4 的五阶段模板，注意「设备桩靠保留前缀被 mir-importer 识别」这一步——本讲的 `KERNEL_PREFIX`/`DEVICE_PREFIX` 正是 u6-l4 流水线能识别设备桩的前提。
- **想看更多 codegen 质量保障手段**：本讲的差分模糊与 u7-l1 的 compile_fail 负向测试、u7-l3 的 Compute Sanitizer 互补——模糊测试抓「算错」，compile_fail 抓「该拒绝的没拒绝」，sanitizer 抓「越界/竞争/未初始化」。三者合起来才是 cuda-oxide 的正确性安全网。
