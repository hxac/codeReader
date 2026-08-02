# 构建系统与 Verilog 生成

## 1. 本讲目标

上一篇（u1-l1）我们已经认识了 Ventus（乘影）的整体架构。本讲要回答一个非常实际的问题：**这一大堆 Chisel（Scala）源码，怎么变成可以仿真、可以综合的 Verilog 文件？**

学完本讲，你应当能够：

1. 读懂本仓库的 `Makefile`，知道 `make init`、`make verilog`、`make fpga-verilog`、`make test` 等命令分别做了什么。
2. 读懂 `build.sc` / `common.sc`，理解基于 **Mill** 的构建系统如何把 Ventus 主模块和它的依赖（hardfloat / fpuv2 / rocket-chip / inclusive-cache / Membox）组织起来。
3. 理解从「Scala 源码 → FIRRTL → firtool 降级 → Verilog」的完整链路，以及仿真用 Verilog 与 FPGA 综合用 Verilog 的两条不同生成路径。
4. 独立跑通 `make init` + `make verilog`，并定位到产物 `GPGPU_top.v`，确认顶层模块名与端口。

## 2. 前置知识

本讲几乎不涉及硬件细节，但需要你先建立下面几个概念。不熟悉的也没关系，我们会结合源码再讲一遍。

- **硬件描述语言（HDL）与 Chisel**：传统上我们用 Verilog 写电路。Chisel 是一种「嵌在 Scala 语言里」的硬件构造语言——你用 Scala 写电路，再由工具把它翻译成 Verilog。所以 Ventus 仓库里的核心文件都是 `.scala`，而不是 `.v`。
- **FIRRTL 与 firtool**：Chisel 把 Scala 代码先「elaborate（展开）」成一种中间表示 **FIRRTL**，再由 CIRCT 项目里的 `firtool` 工具把 FIRRTL **lowering（降级）**成可综合的 Verilog。这条链路可以记作：`Scala(Chisel) → FIRRTL → firtool → Verilog`。
- **Mill**：Scala 生态的构建工具，地位类似 `sbt`/`maven`，但用一份 Scala 脚本 `build.sc` 来声明模块和依赖，启动更快。本仓库根目录自带一个 `mill` 包装脚本，首次运行会自动下载指定版本。
- **git submodule（子模块）**：Ventus 复用了多个外部开源项目（如 rocket-chip、fpuv2），这些项目以 git 子模块形式放在 `dependencies/` 下，需要先 `make init` 拉取，构建才能进行。
- **make / Makefile**：`make` 根据 `Makefile` 里定义的「目标（target）」执行对应命令。本仓库用 Makefile 把一长串 `./mill ...` 命令包装成 `make verilog` 这样的简短命令。

> 提示：本仓库推荐使用 Java 17 及以上版本（README 中注明在 Java 19/21 下测试）。如果本地环境不具备，构建会失败，可以先把本讲当作「源码阅读 + 流程理解」来学。

## 3. 本讲源码地图

本讲涉及的关键文件如下，全部位于仓库根目录或顶层模块入口处：

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile) | 把常用 Mill 命令封装成 `init` / `verilog` / `fpga-verilog` / `test` 等简短目标。 |
| [build.sc](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/build.sc) | Mill 的主构建脚本：声明 Chisel/Scala 版本、各依赖模块、`ventus` 主模块。 |
| [common.sc](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/common.sc) | 被 `build.sc` 通过 `$file` 引入的公共 trait，定义 `HasChisel` 与 `VentusModule`，规定了 ventus 模块的依赖骨架。 |
| [.gitmodules](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/.gitmodules) | 声明 5 个 git 子模块（外部开源依赖）的来源地址。 |
| [ventus/src/top/ExtMem_gen.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/ExtMem_gen.scala) | `make verilog` 真正的执行入口 `GPGPU_gen`：elaborate `GPGPU_top` 并调用 firtool 生成 Verilog。 |
| [ventus/src/top/GPGPU_top.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) | 顶层硬件模块 `class GPGPU_top` 的定义，即生成出的 `GPGPU_top.v` 的来源。 |

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**(4.1) Mill 构建系统与 `ventus` 模块**、**(4.2) Makefile 目标与 Verilog 生成**。前者回答「源码和依赖怎么组织」，后者回答「怎么把它变成 Verilog」。

### 4.1 Mill 构建系统与 ventus 模块

#### 4.1.1 概念说明

Ventus 是一个大型 RTL 工程，但它**不是从零造所有轮子**：浮点运算复用了 `fpuv2`，配置系统（CDE）和部分总线复用了 `rocket-chip`，L2 缓存灵感来自 SiFive 的 inclusive-cache。这些外部代码以 git 子模块形式躺在 `dependencies/` 目录里。

Mill 用一份 `build.sc`（Scala 写的构建脚本）来描述「有哪些模块、每个模块依赖谁、用什么版本的 Chisel/Scala」。理解 `build.sc` 的关键是抓住一张**模块依赖图**：`ventus` 主模块在最顶层，它依赖 `hardfloat`、`fpuv2`、`rocketchip`、`inclusivecache`、`Membox` 这五个子模块；这些子模块又各自指向 `dependencies/` 下对应的源码目录。

为什么用 Mill 而不是 sbt？因为 Chisel 工程往往依赖众多、编译很慢，Mill 的增量编译和模块化对这种场景更友好；而且 `build.sc` 就是普通 Scala，可读性比 sbt 的 `build.sbt` 更接近程序本身。

#### 4.1.2 核心流程

构建一个 Chisel 工程到「可以 elaborate 出 Verilog」的源码状态，流程如下：

1. **拉取子模块**：`make init` 执行 `git submodule update --init --recursive`，把 `dependencies/` 下的 5 个外部项目源码拉下来。
2. **Mill 解析 `build.sc`**：Mill 读入 `build.sc`，并通过 `import $file.common` 引入 `common.sc`，建立模块对象（`ventus`、`rocketchip`、`fpuv2` 等）。
3. **解析 ivy 依赖**：根据 `build.sc` 里的 `v.chiselCrossVersions`，从 Maven 仓库下载 Chisel 6.4.0、Scala 2.13.12、circe 等库。
4. **编译依赖模块**：Mill 按 `moduleDeps` 拓扑顺序，先编译底层依赖（如 `rocketchip`），再编译 `fpuv2`、`inclusivecache`，最后编译 `ventus`。
5. **得到可运行的字节码**：此时 `ventus` 模块已经可以执行其中任意一个 `extends App` 的入口对象（见 4.2）。

模块间的依赖关系可以用下面这张图概括（箭头表示「依赖于」）：

```text
                  ┌─────────── ventus (主模块) ───────────┐
                  │   源码目录: ventus/                    │
                  └──┬───────┬───────┬────────┬──────┬─────┘
                     ▼       ▼       ▼        ▼      ▼
               hardfloat  fpuv2  rocketchip inclusivecache Membox
                  │         │        │           │          │
                  └─────────┴────────┴───┬──────┴──────────┘
                                         ▼
                            dependencies/ (git 子模块源码)
```

#### 4.1.3 源码精读

**(a) 子模块来源 —— `.gitmodules`**

`make init` 拉取的依赖就是这 5 个：

[.gitmodules:1-17](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/.gitmodules#L1-L17) 声明了 `rocket-chip`、`rocket-chip-inclusive-cache`、`berkeley-hardfloat`、`fpuv2`、`Membox2.Scala` 五个子模块及其远程地址。注意 `fpuv2` 还指定了分支 `branch = chisel6`，说明它用的是适配 Chisel 6 的版本。

**(b) 版本号集中定义 —— `object v`**

`build.sc` 用一个 `object v` 集中管理 Scala 与 Chisel 的跨版本矩阵：

[build.sc:20-36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/build.sc#L20-L36) 定义了当前唯一启用的组合是 **Chisel 6.4.0 + Scala 2.13.12**（`3.6.0` / `3.5.0` 两行被注释掉了）。这也是为什么 Makefile 里到处写的是 `ventus[6.4.0]`——`[6.4.0]` 是 Mill 的 cross-version 选择器。

**(c) 公共骨架 —— `common.sc`**

`build.sc` 顶部用 `import $file.common` 引入了公共定义。`common.sc` 不大，但规定了 ventus 模块的依赖骨架：

[common.sc:17-29](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/common.sc#L17-L29) 定义了 `trait VentusModule`，它在 `override def moduleDeps` 里把 `hardfloatModule`、`fpuv2Module`、`rocketchipModule`、`inclusivecacheModule`、`memboxModule` 五个依赖串起来。也就是说，**「ventus 依赖哪五个模块」这件事是在 `common.sc` 里规定的**，`build.sc` 里的 `ventus` 只是具体填上每个依赖指向哪个 Mill 对象。

`HasChisel` trait（[common.sc:7-15](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/common.sc#L7-L15)）则负责给所有需要 Chisel 的模块统一注入 Chisel 的 ivy 依赖和编译器插件。

**(d) ventus 主模块 —— `build.sc`**

真正的「主菜」是 `ventus` 模块定义：

[build.sc:175-216](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/build.sc#L175-L216) 定义了 `object ventus extends Cross[Ventus]`。几个要点：

- `override def millSourcePath = os.pwd / "ventus"`（第 184 行）：告诉 Mill 这个模块的源码根目录是仓库下的 `ventus/`（即 `ventus/src`）。
- 第 186–190 行：把 `common.sc` 里要求的五个依赖名具体绑定到本文件前面定义的 Mill 对象 `hardfloat`、`fpuv2`、`rocketchip`、`inclusivecache`、`MemboxS`。
- 第 191–195 行：额外加入 `circe` 三个库（用于参数导出为 JSON，见 `ParamPrintApp`）。
- 第 197 行：`forkArgs = Seq("-Xmx32G", "-Xss192m")`——elaborate 一个完整 GPGPU 非常吃内存，所以 JVM 堆给到 32G、栈给到 192m。**这也是为什么在内存不足的机器上 `make verilog` 会 OOM**。
- 第 209–215 行：`ventus` 内嵌了一个 `tests` 子模块，对应 `make test`。

**(e) 各依赖模块的源码指向**

每个依赖模块都用 `override def millSourcePath = os.pwd / "dependencies" / ...` 把自己指向 `dependencies/` 下的子模块源码。例如 `fpuv2`：

[build.sc:53-73](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/build.sc#L53-L73) 把 `fpuv2` 指向 `dependencies/fpuv2`，并且内部还嵌套了一个 `fudian` 子模块（浮点基础运算）。`rocketchip`（[build.sc:77-143](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/build.sc#L77-L143)）则更复杂，内部再分出 `macros`、`hardfloat`、`cde`、`diplomomacy`。理解到「依赖模块 = dependencies 子模块源码」即可，不必深究每个内部子模块。

#### 4.1.4 代码实践

**实践目标**：确认构建所需的依赖源码确实存在于 `dependencies/`，并理解 `ventus` 模块指向了 `ventus/`。

**操作步骤**：

1. 在仓库根目录执行 `make init`（若子模块未拉取，会看到下载进度；已拉取则很快完成）。
2. 列出 `dependencies/` 目录，确认 5 个子目录都存在。
3. 执行 `git submodule status`，观察每个子模块当前的 commit。

**预期结果**（待本地验证具体 commit）：

- `dependencies/` 下应能看到 `rocket-chip`、`rocket-chip-inclusive-cache`、`berkeley-hardfloat`、`fpuv2`、`Membox2.Scala` 五个目录。
- `git submodule status` 列出 5 行，每行形如 `<commit> dependencies/xxx`。

**需要观察的现象**：如果某个子模块目录为空或 `make init` 报错，说明没拉成功，后续 `make verilog` 一定失败——这是构建问题最常见的原因。

> 说明：本讲不预设你已经成功联网拉取子模块。若网络受限，可只做「源码阅读型实践」：对照 `.gitmodules` 与 `build.sc` 第 184 行，自己画出「Mill 模块 → 源码目录」的对照表。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `make verilog` 对应的 Mill 命令是 `./mill ventus[6.4.0].run`，而不是 `./mill ventus.run`？

**参考答案**：因为 `ventus` 是一个 Cross 模块（`object ventus extends Cross[Ventus]`），它在 `build.sc` 的 `v.chiselCrossVersions` 里注册了 `6.4.0` 这个交叉版本键。Mill 要求访问 Cross 模块时带上版本选择器 `[6.4.0]`，否则 Mill 不知道你指的是哪个版本的 `ventus`。

**练习 2**：把 `common.sc` 里的 `VentusModule.moduleDeps` 改掉（比如删掉 `fpuv2Module`），会发生什么？

**参考答案**：编译期会报错。因为 `ventus` 源码（例如 FPU 执行单元 `FPUexe`）会 import `fpuv2` 包里的类型；一旦 `fpuv2Module` 不再是依赖，Mill 在编译 `ventus` 时就找不到这些类型，导致编译失败。这印证了 `common.sc` 的依赖列表是构建能通过的「最低配置」。

---

### 4.2 Makefile 目标与 Verilog 生成

#### 4.2.1 概念说明

`build.sc` 解决了「怎么编译源码」，而 `Makefile` 解决了「怎么方便地使用」。Mill 命令往往很长（还要带 cross 版本号），所以仓库用 Makefile 把常用动作封装成 `make xxx`。

Verilog 生成有**两条不同的路径**，对应两种使用场景：

- **`make verilog`（仿真用）**：生成一个（或少数几个）大文件 `GPGPU_top.v`，直接喂给 Verilator 仿真。特点是用 `GPGPU_top` 作为顶层，firtool 把 memory 合在 Verilog 里，方便但不易上板。
- **`make fpga-verilog`（FPGA 综合用）**：以 `GPGPU_axi_adapter_top`（带 AXI 接口的版本）为顶层，先用 firtool 把设计**拆成大量小文件**，并把存储器（SRAM）**单独分离**出来（`mem.conf`），便于在 FPGA 上替换成 BRAM 宏单元。

理解这两条路径的差异，是理解「为什么会有两个 verilog 目标」的关键。

#### 4.2.2 核心流程

**`make verilog` 的执行链路**：

```text
make verilog
   │
   ▼
./mill ventus[6.4.0].run        # Mill 运行 ventus 模块的「主类」
   │
   ▼
top.GPGPU_gen (object extends App)   # 入口对象，见 ExtMem_gen.scala
   │
   ▼
ChiselStage.emitSystemVerilogFile(new GPGPU_top()(...))  # Chisel elaborate 成 FIRRTL
   │
   ▼
firtool lowering   # FIRRTL → SystemVerilog（附带 firtoolOpts 选项）
   │
   ▼
GPGPU_top.v        # 产物：顶层 Verilog
```

**`make fpga-verilog` 的执行链路**：

```text
make fpga-verilog
   │
   ├─① runMain circt.stage.ChiselMain --module top.GPGPU_axi_adapter_top
   │          --target chirrtl --target-dir gen_fpga_verilog/
   │     → 生成 CHIRRTL 中间文件 GPGPU_axi_adapter_top.fir
   │
   ├─② firtool --split-verilog --repl-seq-mem --repl-seq-mem-file=mem.conf
   │     → 把单个 .fir 拆成多个 .v，并把存储器描述写入 mem.conf
   │
   └─③ scripts/gen_sep_mem.sh  scripts/vlsi_mem_gen  mem.conf  gen_fpga_verilog/
         → 根据 mem.conf 为每个存储器生成单独的 .v（便于映射为 FPGA 宏单元）
```

两条路径的差别本质上是 **「firtool 怎么处理存储器」**：仿真路径把存储器留在 Verilog 内部；FPGA 路径把存储器抽出来单独生成。

#### 4.2.3 源码精读

**(a) Makefile 全貌**

[Makefile:1-38](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L1-L38) 是整个构建的入口。按需逐个看关键目标：

- **`init`**：[Makefile:3-4](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L3-L4) —— `git submodule update --init --recursive --progress`，拉取 4.1 里讲到的全部子模块。**这是第一次构建必须先跑的一步**。
- **`compile`**：[Makefile:17-18](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L17-L18) —— `./mill -i -j 0 __.compile`，只编译所有模块、不运行，适合快速检查语法。
- **`test`**：[Makefile:20-23](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L20-L23) —— 运行 chiseltest 单元测试 `play.AdvancedTest`。**注意 README 已说明 `make test` 被废弃（deprecated）**，波形在 `test_run_dir`，正式仿真改用 `sim-verilator/`（详见 u1-l4）。
- **`verilog`**：[Makefile:25-26](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L25-L26) —— `./mill ventus[6.4.0].run`，生成仿真用 `GPGPU_top.v`。
- **`fpga-verilog`**：[Makefile:28-31](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L28-L31) —— 三步走，生成 FPGA 综合用 Verilog（见 4.2.2 的链路图）。
- **`clean`**：[Makefile:33-34](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L33-L34) —— 清理 `out/`、`test_run_dir/`、`.idea/`（Mill 编译产物在 `out/`，注意它不清 `GPGPU_top.v` 和 `gen_fpga_verilog/`）。

**(b) Verilog 生成的真正入口 —— `GPGPU_gen`**

Mill 的 `run` 任务会执行模块里被识别为「主类（main class）」的入口对象。在 Scala 中，`object X extends App` 会自动生成一个 `main(args: Array[String])` 方法，因此可以被 Mill 当作主类运行。生成仿真用 `GPGPU_top.v` 的主类是 `top.GPGPU_gen`：

[ventus/src/top/ExtMem_gen.scala:22-31](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/ExtMem_gen.scala#L22-L31) 做了三件事：

1. 第 23 行：用 `MyConfig` 构造 L1 缓存的参数实例 `L1param`。
2. 第 24 行：用 `InclusiveCacheParameters_lite(...)` 构造 L2 缓存的参数实例 `L2param`（用到了 `num_l2cache`、`l2cache_NSets` 等参数）。
3. 第 25 行：`ChiselStage.emitSystemVerilogFile(new GPGPU_top()(L1param, SV = Some(mmu.SV32)), firtoolOpts = ...)`——这就是真正「展开电路 → 生成 Verilog」的一行。它把上面两个参数实例隐式传给 `GPGPU_top`，并指定 SV32 作为可选 MMU 的页表模式（MMU 默认关闭，这里只是准备好参数）。

第 26–30 行的 `firtoolOpts` 三个选项也值得知道：

- `--disable-mem-randomization` / `--disable-reg-randomization`：关闭 Verilog 里 mem/reg 的随机初值（仿真时更可控）。
- `-lowering-options=disallowLocalVariables`：禁止生成 `automatic logic` 这类 SystemVerilog 局部变量语法，让产物尽量是纯 Verilog（代码里有注释说明，便于老式综合工具处理）。

> 注意：仓库里其实有多个 `object ... extends App`（例如 `Mem_SimWrapper.scala` 里的 `emitVerilog`、`parameters.scala` 里的 `ParamPrintApp`）。`make verilog` 依赖的是 Mill 对主类的检测与配置，最终产物 `GPGPU_top.v` 由 `GPGPU_gen` 负责生成——这是从「产物文件名」反推确定的。若要显式运行某个指定主类，应使用 `runMain <主类全名>`，`make fpga-verilog` 就是用 `--main circt.stage.ChiselMain` 显式指定主类的（见 [Makefile:29](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/Makefile#L29)）。

**(c) 产物顶层模块 —— `class GPGPU_top`**

生成的 `GPGPU_top.v` 的内容来自 Scala 里的 `class GPGPU_top`：

[ventus/src/top/GPGPU_top.scala:150-162](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L150-L162) 定义了顶层模块 `class GPGPU_top(implicit p: Parameters, FakeCache: Boolean = false, SV: Option[mmu.SVParam] = None)` 及其端口：

- `host_req`（第 154 行）：`Flipped(DecoupledIO(new host2CTA_data))`——Host 向 GPU 派发 workgroup 的请求通道。
- `host_rsp`（第 155 行）：`DecoupledIO(new CTA2host_data)`——GPU 向 Host 回报 workgroup 完成。
- `out_a`（第 156 行）：`Vec(NL2Cache, Decoupled(new TLBundleA_lite(l2cache_params)))`——L2 向外存的请求（TileLink A 通道）。
- `out_d`（第 157 行）：`Flipped(Vec(NL2Cache, Decoupled(new TLBundleD_lite(l2cache_params))))`——外存返回 L2 的响应（TileLink D 通道）。
- `icache_invalidate`（第 162 行）：`Input(Bool())`——指令缓存失效信号。

这正好对应 u1-l1 讲过的「顶层端口分两组：host_req/host_rsp 接 CPU，out_a/out_d 经 AXI4Adapter 接 DDR」。

#### 4.2.4 代码实践

**实践目标**：跑通 `make verilog`，定位产物 `GPGPU_top.v`，记录顶层模块名并查看端口列表，确认构建成功。

**操作步骤**：

1. 确保已执行 `make init`，且本机有 Java 17+、约 32G 可用内存（JVM 堆）。
2. 在仓库根目录执行：
   ```bash
   make verilog
   ```
3. 构建结束后，定位产物文件：
   ```bash
   find . -name "GPGPU_top.v" -not -path "*/dependencies/*"
   ```
4. 查看顶层模块定义与端口：
   ```bash
   grep -n "^module GPGPU_top" <产物路径>/GPGPU_top.v   # 或直接打开文件顶部
   ```

**需要观察的现象**：

- `make verilog` 过程中会看到 Mill 编译各模块，最后由 firtool 输出 Verilog；末尾通常能看到「Done compiling」或文件写出相关日志。
- 产物 `GPGPU_top.v` 中应以 `module GPGPU_top` 开头，其端口列表应包含 `host_req_*` / `host_rsp_*` / `out_a_*` / `out_d_*` 等信号。

**预期结果**：找到 `GPGPU_top.v`，其顶层模块名是 `GPGPU_top`，端口对应 4.2.3(c) 列出的那几组（host 与 out_a/out_d）。如果 `make verilog` 在 elaborate 阶段因内存不足被 kill（OOM），可尝试在内存更大的机器上重试，这印证了 `build.sc` 第 197 行 `-Xmx32G` 的必要性。

> 说明：由于本环境未实际执行构建，上述命令的精确输出为「待本地验证」。若你无法运行，请改为**源码阅读型实践**：打开 `ExtMem_gen.scala` 的 `GPGPU_gen`，确认它 elaborate 的是 `GPGPU_top`；再打开 `GPGPU_top.scala` 第 150–162 行，手抄一遍端口列表。

#### 4.2.5 小练习与答案

**练习 1**：`make verilog` 和 `make fpga-verilog` 的顶层模块分别是哪个？为什么不一样？

**参考答案**：`make verilog` 的顶层是 `GPGPU_top`（见 `ExtMem_gen.scala` 第 25 行 `new GPGPU_top(...)`）；`make fpga-verilog` 的顶层是 `top.GPGPU_axi_adapter_top`（见 `Makefile` 第 29 行 `--module top.GPGPU_axi_adapter_top`）。不同是因为用途不同：仿真用裸 `GPGPU_top` 即可，而 FPGA 需要带 AXI 接口、便于和外部总线对接的 `GPGPU_axi_adapter_top`，并且还要把存储器分离出来映射成 BRAM。

**练习 2**：`make fpga-verilog` 第三步 `scripts/gen_sep_mem.sh` 的输入 `mem.conf` 是谁产生的？它的作用是什么？

**参考答案**：`mem.conf` 是第二步 `firtool --repl-seq-mem --repl-seq-mem-file=mem.conf` 产生的。`--repl-seq-mem` 让 firtool 把设计里的顺序存储器（SRAM）「外部化」——不再写死在 Verilog 里，而是把每个存储器的规格（位宽、深度、端口数等）记录到 `mem.conf`。`gen_sep_mem.sh` 再配合 `vlsi_mem_gen` 为每个存储器生成单独的 `.v`，这样在 FPGA/ASIC 流程里就能把它们替换成具体的 BRAM 或 SRAM 宏单元。

**练习 3**：如果你只想检查 Scala 源码能否编译、不想生成完整 Verilog，应该用哪个命令？

**参考答案**：`make compile`，它等价于 `./mill -i -j 0 __.compile`，只编译不运行，速度快得多。

---

## 5. 综合实践

把本讲的两个最小模块串起来，完成一次「从零到 Verilog」的全流程，并填写下面的构建产物清单表。

**任务**：

1. 执行 `make init`，确认 5 个子模块拉取成功（`git submodule status`）。
2. 执行 `make compile`，确认源码能编译通过（比 `make verilog` 快）。
3. 执行 `make verilog`，生成 `GPGPU_top.v`。
4. 用文本编辑器/`grep` 查看 `GPGPU_top.v`，记录：顶层模块名、`host_req`/`host_rsp`/`out_a`/`out_d` 端口是否齐全、文件总行数（量级）。
5. 填写下面这张表（行数等数值为「待本地验证」）：

| 步骤 | 命令 | 产物 | 关键产物位置 / 说明 |
| --- | --- | --- | --- |
| 拉依赖 | `make init` | `dependencies/*` 源码 | 5 个子模块 |
| 仅编译 | `make compile` | `out/`（Mill 编译结果） | 不产生 Verilog |
| 仿真 Verilog | `make verilog` | `GPGPU_top.v` | 顶层模块 `GPGPU_top` |
| FPGA Verilog | `make fpga-verilog` | `gen_fpga_verilog/*.v` + `mem.conf` | 顶层 `GPGPU_axi_adapter_top`，存储器已分离 |

6. （可选）对比 `make verilog` 与 `make fpga-verilog` 的产物数量：前者是少数几个大文件，后者是几十上百个小文件加上 `mem.conf`。思考这种差异如何影响后续仿真（用大文件）与综合（用拆分+宏单元）的选择。

> 如果本机环境无法完整运行上述命令，请至少完成「源码阅读版」：对照本讲的源码精读，手工填写上表的「产物」「说明」两列，并能在 `build.sc` / `Makefile` / `ExtMem_gen.scala` 中指出每一步对应的代码行。

## 6. 本讲小结

- Ventus 的构建基于 **Mill**：`build.sc` 声明模块与依赖，`common.sc` 规定 `ventus` 依赖 `hardfloat`/`fpuv2`/`rocketchip`/`inclusivecache`/`Membox` 这五个子模块（源码在 `dependencies/`）。
- 首次构建必须先 `make init` 拉取 git 子模块，否则后续都会失败。
- Verilog 生成链路是 `Scala(Chisel) → FIRRTL → firtool lowering → Verilog`；Chisel 6.4.0 / Scala 2.13.12 是当前唯一启用的版本组合，这也是命令里 `ventus[6.4.0]` 的由来。
- `make verilog` 的入口是 `top.GPGPU_gen`（`ExtMem_gen.scala`），它 elaborate `GPGPU_top` 并产出一个仿真用的 `GPGPU_top.v`。
- `make fpga-verilog` 走另一条路：以 `GPGPU_axi_adapter_top` 为顶层，firtool `--split-verilog` + `--repl-seq-mem` 把存储器分离成 `mem.conf`，再用 `gen_sep_mem.sh` 单独生成，便于 FPGA 综合时替换为 BRAM 宏单元。
- `make test`（chiseltest）已废弃，正式仿真改用 `sim-verilator/`（下一阶段的 u1-l4 会讲）。elaborate 很吃内存（`build.sc` 里 `-Xmx32G`），内存不足会导致 `make verilog` OOM。

## 7. 下一步学习建议

现在你已经能把源码变成 Verilog 了，下一步建议：

1. **u1-l3（目录结构与源码组织）**：在深入读 Verilog 之前，先建立对 `ventus/src` 各子目录（top / pipeline / cta / L1Cache / L2cache / mmu / axi）职责的整体认知，为后续逐模块精读做地图。
2. **u1-l4（Verilator 仿真与测试用例）**：用刚生成的 `GPGPU_top.v`（或 `sim-verilator` 框架）真正跑通一个 `vecadd` 测试，看到硬件动起来，这是对「构建成功」最有说服力的验证。
3. 若你对构建系统本身感兴趣，可以继续读 `build.sc` 里 `rocketchip` 模块（[build.sc:77-143](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/build.sc#L77-L143)），理解一个复杂依赖是如何再拆分成 `macros`/`cde`/`diplomacy` 等内部子模块的。
