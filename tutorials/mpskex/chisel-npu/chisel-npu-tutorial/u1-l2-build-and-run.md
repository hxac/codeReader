# 开发环境与构建运行方式

## 1. 本讲目标

上一讲我们建立了对 chisel-npu 的全局认识,知道它是一段「用 Chisel 写的 NPU 硬件描述」。这一讲要回答一个非常实际的问题:**这一段 Scala/Chisel 源码,怎么变成一块可以仿真、可以烧进 FPGA 的硬件?**

学完本讲,你应当能够:

- 说出 `make image / container / test / build / build-sc / docs` 这些常用目标分别做什么。
- 看懂 `build.sbt` 里的关键配置:Chisel 6.7.0、Scala 2.13.12、chiseltest 5.0.2 以及编译插件。
- 理解为什么这个项目「优先用 Docker,而不是裸机装 sbt」——以及 Docker 镜像里到底打包了哪些工具(firtool / verilator / SystemC)。
- 知道 Vivado FPGA 构建入口 `build-fpga` 在哪里、和 Docker 流程有何不同。
- 亲手跑一次 `make build`,并发现一个容易被忽视的小陷阱:`make` 目标叫 `top.v`,而 `sbt run` 实际产出的文件叫 `top.sv`。

## 2. 前置知识

在进入源码之前,先建立三个直觉。这些概念是理解本讲所有命令的前提。

**(1) 什么是 Chisel 的「elaborate(精细化)」?**
Chisel 不是一门独立的 HDL,它是 Scala 的一个库。你写的 Scala 代码在运行时会构造出一棵硬件中间表示(FIRRTL),再由 `firtool`(CIRCT 项目提供的编译器)翻译成 SystemVerilog。所以「构建」一词在这个项目里其实是:**运行一段 Scala 程序,让它吐出 `.sv` 文件**。

**(2) sbt 是什么?**
sbt(Scala Build Tool)是 Scala 的构建工具,地位类似 Java 的 Maven/Gradle、Rust 的 Cargo。它读取 `build.sbt` 来决定用哪个 Scala 版本、引入哪些依赖、加哪些编译选项。本项目的核心命令 `sbt run` 和 `sbt test` 都由它驱动。

**(3) 为什么用 Docker?**
本项目的工具链很重:除了 sbt 和 Chisel 本身,还需要 `firtool 1.62.1`、`verilator v5.036`、`SystemC 3.0.1`,而且 Chisel 6.7.0 还得在本地 `publishLocal`。这些版本稍有错配就会构建失败。把这些工具连同正确版本一起打包进一个 Docker 镜像,任何人 `docker run` 就能拿到完全一致的环境——这就是为什么 `AGENTS.md` 明确建议:**优先用 Docker,不要裸机跑 sbt**。

> 约定:本讲里出现的命令如果没有特别说明,都是在「宿主机」上执行。`make` 会替你把这些命令转发进 Docker 容器。

## 3. 本讲源码地图

本讲涉及的文件不多,但每一个都直接关系到「怎么构建、怎么跑」:

| 文件 | 作用 |
|:---|:---|
| `build.sbt` | sbt 工程定义:Scala 版本、Chisel 依赖、编译选项、测试选项。 |
| `project/build.properties` | 固定 sbt 自身的版本为 1.9.7。 |
| `project/plugin.sbt` | sbt 日志级别设置。 |
| `docker/dockerfile` | 构建 `fangruil/chisel-dev` 开发镜像的配方,打包整套工具链。 |
| `Makefile` | 所有 `make xxx` 目标的定义,本质是对 `docker run` 的封装。 |
| `src/main/scala/top/top.scala` | 顶层入口 `Main`,调用 `ChiselStage.emitSystemVerilog` 产出 `top.sv`。 |
| `AGENTS.md` | 仓库的「避坑指南」,记录了工具链版本与若干 gotcha。 |
| `.github/workflows/actions.yml` | CI 流水线定义,展示官方推荐的命令序列。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块:先看整条工具链是怎么串起来的(4.1),再看 sbt 工程是怎么定义的(4.2),接着看 Docker 镜像里装了什么(4.3),最后看 Makefile 如何把这一切封装成 `make` 命令(4.4)。

### 4.1 构建工具链总览:从 Scala 源码到 top.sv

#### 4.1.1 概念说明

把 chisel-npu 的构建链路画出来,大致是这样一条流水线:

```
 Scala/Chisel 源码
        │  sbt run(运行 top.Main)
        ▼
   FIRRTL 中间表示
        │  firtool(CIRCT)
        ▼
     top.sv (SystemVerilog)
        │
        ├──► verilator / SystemC  → 软件仿真
        └──► Vivado               → 综合、上 FPGA
```

这条链路里有三个关键角色:

- **sbt**:负责把 Scala 源码编译成可运行的 `.class`,然后执行 `top.Main`。
- **firtool**:CIRCT 项目提供的 FIRRTL 编译器,把硬件中间表示翻译成 SystemVerilog。它必须存在于 `PATH` 中(镜像里设了 `CHISEL_FIRTOOL_PATH=/usr/local/bin`)。
- **verilator / Vivado**:消费 `top.sv` 的下游工具。前者做软件仿真,后者做 FPGA 综合。

`AGENTS.md` 把这条链路的版本要求讲得很清楚,这也是本模块最权威的参考:

[AGENTS.md:6-9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/AGENTS.md#L6-L9) —— 中文解读:Chisel 6.7.0 / Scala 2.13.12 / sbt 1.9.7,额外需要 firtool 1.62.1、verilator v5.036、SystemC 3.0.1,且这些工具都由 `fangruil/chisel-dev` 镜像提供;裸机 sbt 通常跑不起来,除非你自己 `publishLocal` 了 Chisel 6.7.0。

#### 4.1.2 核心流程

构建与运行的「主链路」可以用下面这组步骤描述:

1. 宿主机执行 `make build`。
2. Makefile 内部展开成 `docker run ... fangruil/chisel-dev:<arch> sbt run`。
3. 容器内 sbt 读取 `build.sbt`,解析 Chisel 6.7.0 依赖、加载 chisel-plugin 编译插件。
4. sbt 编译 `src/main/scala/` 下的源码,找到 `top.Main`(它 `extends App`)。
5. `Main` 调用 `ChiselStage.emitSystemVerilog(...)`,触发 elaborate 与 firtool 翻译。
6. `Main` 用 `Files.write` 把结果写到仓库根目录的 `top.sv`。

注意第 6 步:**产出文件名是 `top.sv`,而不是 Makefile 目标名暗示的 `top.v`**。这是本讲的第一个「小陷阱」,我们会在 4.4 和综合实践里专门验证它。

CI 流水线把这条主链路原样跑了一遍,只是把 `make build` 换成了直接的 `sbt run`:

[.github/workflows/actions.yml:19-37](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/.github/workflows/actions.yml#L19-L37) —— 中文解读:`Build` 作业在 `fangruil/chisel-dev:amd64` 容器里执行 `sbt run`,`Test` 作业依赖 `Build` 之后再执行 `sbt test`。也就是说 CI 的命令序列就是「先 elaborate 出 RTL,再跑测试」。

#### 4.1.3 源码精读

链路的终点站就是 `top.Main`,它只有几行,却完成了 elaborate + 落盘两件事:

[src/main/scala/top/top.scala:11-19](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/top/top.scala#L11-L19) —— 中文解读:`object Main extends App` 是 sbt 的入口;sbt 看到 `run` 就会找一个带 `main` 方法的对象,`App` trait 自动提供。第 14 行 `ChiselStage.emitSystemVerilog(new MMALU(new MMPE(), 32, 8, 32), ...)` 对一个 `MMALU` 实例做 elaborate;`firtoolOpts` 关掉了随机化和调试信息以产出干净的 RTL。第 18 行 `Files.write(Paths.get("top.sv"), ...)` 把字符串写进**当前工作目录下的 `top.sv`**——这正是 `sbt run` 实际产出文件名的来源。

> 关于规模:这里 elaborate 的是 `MMALU(new MMPE(), 32, 8, 32)`,第二参数 `32` 是脉动阵列边长。上一讲提到根目录 `top.sv` 目前只 elaborate 出 MMALU(完整的 `NCoreBackend` 定义在 `SimpleBackend.scala`),原因就在这里——`Main` 只实例化了 MMALU。

#### 4.1.4 代码实践

**实践目标**:不实际运行,只通过阅读 CI 配置和入口源码,推断「官方推荐的命令」与「产出文件名」。

**操作步骤**:
1. 打开 `.github/workflows/actions.yml`,确认 CI 的两条核心命令。
2. 打开 `src/main/scala/top/top.scala`,找到 `Files.write` 的目标文件名。
3. 对照 `AGENTS.md` 的 Toolchain 小节,核对工具版本。

**需要观察的现象 / 预期结果**:
- CI 顺序是 `sbt run` 然后 `sbt test`,二者都在 `fangruil/chisel-dev:amd64` 容器里。
- `top.Main` 写出的文件是 `top.sv`(注意扩展名是 `.sv`,不是 `.v`)。
- 如果你的环境里没有 Docker,这一步只能「读代码推断」,实际验证留到综合实践(或标注「待本地验证」)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 CI 要先 `sbt run` 再 `sbt test`,而不是反过来?
> **参考答案**:`sbt run` 负责 elaborate 出 RTL(`top.sv`),验证「硬件能正确生成」;`sbt test` 负责跑仿真测试。虽然测试内部也会按需 elaborate,但分开作业可以让「构建失败」和「测试失败」在 CI 里清晰区分,且 `Test` 作业 `needs: Build`,构建不通过就不会浪费时间跑测试。

**练习 2**:`top.Main` 的 `firtoolOpts` 去掉了 `-disable-all-randomization` 和 `-strip-debug-info` 之外的什么?这两个选项分别有什么作用?
> **参考答案**:`-disable-all-randomization` 关闭 FIRRTL 默认对未初始化寄存器的随机化(让仿真波形更确定、可读);`-strip-debug-info` 去掉源码定位等调试信息,减小产出文件体积。两者都是为了得到干净、体积可控的 `top.sv`。

### 4.2 build.sbt:Chisel 6.7.0 与 Scala 2.13.12 的依赖配置

#### 4.2.1 概念说明

`build.sbt` 是 sbt 的工程定义文件。它回答几个问题:用什么语言版本?引入哪些库?启用哪些编译选项?怎么跑测试?本项目是从 Chisel 官方模板生成的,所以保留了一些模板痕迹——这一点我们会在源码精读里看到。

`sbt` 之外还有两个小文件:`project/build.properties` 把 sbt 自身钉死在 1.9.7([project/build.properties:1](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/project/build.properties#L1)),`project/plugin.sbt` 只设了日志级别([project/plugin.sbt:1](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/project/plugin.sbt#L1))。这种「sbt 版本与构建定义分离」是 sbt 的惯例。

#### 4.2.2 核心流程

`build.sbt` 被加载时,sbt 会:

1. 读取 `ThisBuild` 作用域的全局设置(scalaVersion、version、organization)。
2. 定义一个名为 `root` 的工程,指向当前目录。
3. 解析 `libraryDependencies` 里的 Chisel 6.7.0 与 chiseltest 5.0.2,从 Coursier 缓存(镜像里映射到 `/workspace/.cache/coursier/v1`)拉取。
4. 加载 `chisel-plugin` 编译插件——这是 Chisel 6 必须的宏插件。
5. 应用 `scalacOptions` 和测试选项。

依赖解析阶段是网络密集的,所以镜像把 Coursier 缓存挂载到了仓库的 `.cache/` 目录,二次构建能复用缓存。

#### 4.2.3 源码精读

[build.sbt:3-7](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L3-L7) —— 中文解读:`scalaVersion := "2.13.12"`、`version := "0.1.0"`,以及把 Chisel 版本抽成一个 `val chiselVersion = "6.7.0"`,方便后面统一引用。

[build.sbt:9-15](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L9-L15) —— 中文解读:`root` 工程定义。依赖有两条:`org.chipsalliance %% chisel % 6.7.0`(主依赖)和 `edu.berkeley.cs %% chiseltest % 5.0.2 % "test"`(仅测试用)。注意 `%%` 表示会按当前 Scala 版本选取对应的 artifact。

> **模板痕迹 gotcha**:`name := "%NAME%"`、`organization := "%ORGANIZATION%"` 是 Chisel 模板里没有替换掉的占位符。`AGENTS.md` 特别提醒:sbt 能接受它们,不要去改它,除非你真的要重命名工程。

[build.sbt:16-23](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L16-L23) —— 中文解读:`scalacOptions` 启用了 `-language:reflectiveCalls`、`-deprecation`、`-feature`、`-Xcheckinit`、`-Ymacro-annotations`(Chisel 宏注解需要它);`addCompilerPlugin("org.chipsalliance" % "chisel-plugin" % chiselVersion cross CrossVersion.full)` 注入 Chisel 6 必需的编译器插件。

[build.sbt:25](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/build.sbt#L25) —— 中文解读:`Test / testOptions += Tests.Argument(..., "-oDT")`,这是 ScalaTest 的报告选项,作用是**按耗时从慢到快打印每个测试用例**,对定位慢测试很有用。

#### 4.2.4 代码实践

**实践目标**:验证「改 build.sbt 不会破坏构建,但会触发 sbt 重新加载」。

**操作步骤**:
1. 打开 `build.sbt`,在第 1 行 `// See README.md for license details.` 这条**注释**里随便加几个字(比如改成 `// See README.md for license details. (build env lecture)`)。
2. 在容器里执行 `sbt run`(或宿主机 `make build`)。

**需要观察的现象 / 预期结果**:
- sbt 检测到 `build.sbt` 变化,会打印类似 `[info] Reloading...` 的日志,然后重新加载工程定义。
- 由于 `Main` 无条件 `Files.write("top.sv")`,只要 `sbt run` 成功,`top.sv` 就会被重新生成。
- 改的是注释,不会影响任何编译结果,所以构建依然应当成功。

**说明**:如果环境里没有 Docker 或镜像未拉取,此步为「待本地验证」;你可以先只做第 1 步(改注释),把「预期现象」记下来,等环境就绪再核对。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `chiseltest` 依赖后面有 `% "test"`,而 `chisel` 没有?
> **参考答案**:`% "test"` 把该依赖限制在测试 classpath 上,不会打进最终产物。`chiseltest` 只在测试时用,而 `chisel` 是主源码必须的,所以不限定配置。

**练习 2**:`-Ymacro-annotations` 这个选项去掉会怎样?
> **参考答案**:Chisel 大量使用宏(包括 `@chiselName` 等注解和派生代码生成)依赖 Scala 2.13 的宏注解支持;去掉它会导致 Chisel 相关的宏展开失败,编译报错。

### 4.3 Dockerfile 与 docker 环境变量

#### 4.3.1 概念说明

`docker/dockerfile` 构建出来的镜像 `fangruil/chisel-dev` 就是本项目的「一站式工具箱」。它最了不起的地方在于:**几乎所有工具都是从源码编译的**——firtool 来自 CIRCT 源码,verilator 从 git tag v5.036 编译,Chisel 6.7.0 也是 clone 下来 `publishLocal` 的。这样做的好处是版本完全可控、可复现;代价是镜像构建时间很长,所以正常使用时**直接 `docker pull` 这个镜像,而不是自己 `make image`**(除非你要改镜像)。

#### 4.3.2 核心流程

镜像采用多阶段、分架构的构建思路:

1. 以 `ubuntu:24.04` 为底,按 `TARGETARCH`(amd64 / arm64)切到不同的 apt 源(清华镜像)。
2. 安装基础编译工具:JDK、make、autoconf、g++、flex、bison、ninja、cmake 等。
3. **构建 firtool 1.62.1**:下载 CIRCT 全源码,先编 LLVM+MLIR,再编 CIRCT,最后把 `build/bin/*` 搬到 `/usr/local/bin/`。
4. **构建 verilator v5.036**:`git checkout v5.036 && autoconf && ./configure && make`。
5. 通过 coursier 安装 sbt(amd64 与 arm64 用不同的 coursier 二进制)。
6. **构建 Chisel 6.7.0**:`git checkout v6.7.0 && sbt compile && sbt "unipublish / publishLocal"`,让本地 Ivy 仓库里有 6.7.0 的 Chisel。
7. **安装 SystemC 3.0.1** 到 `/opt/systemc`。
8. 设置若干环境变量,供后续 Chisel / verilator 查找工具。

#### 4.3.3 源码精读

[docker/dockerfile:20-44](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docker/dockerfile#L20-L44) —— 中文解读:这就是 firtool 的「从源码编译」段落。先用 Ninja 构建 LLVM(只构建 host target、开 assertion、release),再基于该 LLVM 构建 CIRCT,最后把产物二进制 `mv` 到 `/usr/local/bin/`。

[docker/dockerfile:84](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docker/dockerfile#L84) —— 中文解读:verilator 的安装固定在 tag `v5.036`,走经典的 `autoconf && ./configure && make -j8 && make install` 流程。

[docker/dockerfile:103](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docker/dockerfile#L103) —— 中文解读:Chisel 6.7.0 通过 `sbt "unipublish / publishLocal"` 发布到本地,这样本项目 `build.sbt` 里 `libraryDependencies` 解析 Chisel 6.7.0 时就能命中本地仓库——这正是「裸机 sbt 跑不起来」的根本原因:裸机环境里没有这个本地发布。

[docker/dockerfile:109-111](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docker/dockerfile#L109-L111) —— 中文解读:SystemC 3.0.1 装到 `/opt/systemc`(静态库 `BUILD_SHARED_LIBS=OFF`)。

最关键的是结尾这几行环境变量,它们是 Chisel 和 verilator 找到工具的「路标」:

[docker/dockerfile:115-118](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docker/dockerfile#L115-L118) —— 中文解读:`COURSIER_CACHE=/workspace/.cache/coursier/v1` 把依赖缓存挂到挂载点,便于复用;`CHISEL_FIRTOOL_PATH=/usr/local/bin` 告诉 Chisel 去哪找 firtool;`SYSTEMC_INCLUDE` 与 `SYSTEMC_LIBDIR` 指向 `/opt/systemc` 下的头文件和库,供 SystemC 后端使用。

#### 4.3.4 代码实践

**实践目标**:进入容器,确认工具链版本与上一讲/`AGENTS.md` 的描述一致。

**操作步骤**:
1. 在宿主机执行 `make container`(见 4.4),进入交互式 bash。
2. 在容器里依次执行:
   - `firtool --version`
   - `verilator --version`
   - `sbt --version`(或 `sbt sbtVersion`)
   - `ls /opt/systemc`

**需要观察的现象 / 预期结果**:
- `firtool` 版本应为 `firtool-1.62.1`。
- `verilator` 版本应为 `v5.036`。
- sbt 版本应为 1.9.7。
- `/opt/systemc` 下应有 `include` 与 `lib` 两个子目录。

**说明**:无 Docker 环境时此步为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**:`COURSIER_CACHE` 为什么要指向 `/workspace/.cache/...` 而不是默认的 `$HOME`?
> **参考答案**:容器以 `-v $PWD:/workspace` 把仓库挂载进来,把缓存放在 `/workspace/.cache/` 意味着它落在宿主机文件系统上,容器销毁后依然保留,下次 `make` 能直接复用,避免重复下载 Chisel / Scala 依赖。

**练习 2**:为什么说「自己 `make image` 很慢,日常应直接 `docker pull`」?
> **参考答案**:镜像里要从源码编译 LLVM+CIRCT(firtool)、verilator、SystemC,还要 `publishLocal` Chisel,这些编译动辄几十分钟到数小时。日常使用只需拉取已构建好的镜像;只有要修改工具链版本时才需要本地重新 `make image`。

### 4.4 Makefile:目标定义与构建运行命令

#### 4.4.1 概念说明

`Makefile` 是本项目对外的「命令面板」。它的核心思想很简单:**把所有 sbt 命令都包进 `docker run` 里**,这样用户只需 `make xxx`,不用关心 Docker 参数。理解了这一点,Makefile 读起来就非常轻松。

文件顶部设置了三个会影响所有目标的变量:

[Makefile:3-9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L3-L9) —— 中文解读:`ARCH` 取自 `uname -m`(注意 `x86_64` 会在目标里被重映射成 `amd64`);`VER=0.4` 是镜像的版本标签(即 `fangruil/chisel-dev:amd64-0.4`);`SBT_OPTS="-Xmx8G -Xss2M"` 给 JVM 分配 8GB 堆和 2MB 栈,Chisel elaborate 很吃内存。

#### 4.4.2 核心流程

下表把最常用的目标列出来,左边是命令,中间是它展开后的实质,右边是用途:

| 命令 | 实质 / 关键行为 | 用途 |
|:---|:---|:---|
| `make image` | `docker build docker -t fangruil/chisel-dev:<arch>` | 构建(或重建)开发镜像 |
| `make container` | `docker run -i -v $PWD:/workspace ... bash` | 进容器开交互式 shell |
| `make test` | `docker run ... sbt test` | 在镜像里跑全部测试 |
| `make build` | 等价于 `make top.v` → `docker run ... sbt run` | elaborate,产出 `top.sv` |
| `make build-sc` | 在 `build` 之后跑 `verilator top.v -sc` | 生成 SystemC 仿真模型 |
| `make docs` | `pip3 install ... && mkdocs serve`(在**宿主机**) | 本地预览文档站点 |
| `make clean` | 删除 `target`、`*.v`、`*.anno.json` 等 | 清理构建产物(不删 `top.sv`) |
| `make clean-cache` | 删除 `.cache/` | 清理 Coursier 缓存 |

FPGA 相关目标(`build-fpga` / `build-fpga-debug` / `build-fpga-clean`)走的是另一条路——**不经过 Docker,而是直接调用宿主机上的 Vivado**,我们放在本模块最后讲。

#### 4.4.3 源码精读

**镜像与容器**:

[Makefile:11-21](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L11-L21) —— 中文解读:`image` 目标按 `ARCH` 分派到 `image-amd64` 或 `image-arm64`,后者执行 `docker build docker -t fangruil/chisel-dev:<arch> -t fangruil/chisel-dev:<arch>-0.4`。注意 `image-x86_64` 被显式重映射到 `image-amd64`。

[Makefile:23-25](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L23-L25) —— 中文解读:`container` 用 `-u $(id -u):$(id -g)` 以当前用户身份运行(避免产出文件变成 root),`-v ${PWD}:/workspace/` 把仓库挂进容器的 `/workspace`,`--rm` 退出即删容器。

**测试与构建(本讲最重要的几行)**:

[Makefile:27-28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L27-L28) —— 中文解读:`test` 把 `SBT_OPTS` 通过 `--env` 传进容器,执行 `sbt test`。

[Makefile:30-36](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L30-L36) —— 中文解读:**这里是本讲最大的陷阱**。目标名叫 `top.v`(第 30 行),它执行 `sbt run`(第 31 行);`build: top.v` 与 `build-sc: top.v` 都依赖它。但 `top.Main` 实际写出的文件是 `top.sv`(见 4.1.3),**`top.v` 这个文件永远不会被生成**。后果是:make 永远认为 `top.v`「过期」,于是每次 `make build` 都会重新 `sbt run`。`build-sc` 还有个额外坑:它对 `top.v` 跑 verilator,但实际文件是 `top.sv`,需要先改名或打补丁(详见 `AGENTS.md` 的 `make build-sc` 条目)。

**Vivado FPGA 构建入口(宿主机工具,不走 Docker)**:

[Makefile:38-48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L38-L48) —— 中文解读:`VIVADO ?= $(HOME)/Xilinx/2025.2/Vivado/bin/vivado`、`CHIP ?= xc7k480t`、`VIVADO_LOGDIR ?= build`。`build-fpga` 先依赖 `top.v`(即先生成 RTL),然后调用 Vivado 批处理模式跑 `ip/vivado/$(CHIP)/scripts/build_npu.tcl`,日志写到 `build/`。`build-fpga-debug` 改用带 ILA(逻辑分析仪)的 `build_npu_with_ila.tcl`。这部分要求宿主机装了 Vivado 2025.2,是 FPGA 上板流程的入口(细节留到 U8 讲)。

**文档与清理**:

[Makefile:99-101](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L99-L101) —— 中文解读:`docs` **不在 Docker 里跑**,而是宿主机直接 `pip3 install -r docs/requirements.txt && python3 -m mkdocs serve` 起一个本地文档站点。

[Makefile:103-107](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/Makefile#L103-L107) —— 中文解读:`clean` 删 `target`、`*.v`、`*.anno.json` 和 `VIVADO_LOGDIR`,但**不删 `top.sv`**(它是检入仓库的、约 17MB 的大文件,`*.v` 通配符匹配不到 `.sv`);`clean-cache` 才会清 `.cache/`。

> 补充:仓库还提供了单测快捷脚本 `tool/test-specific-spec.sh <全限定 Spec 名>`,它展开为 `docker run ... sbt "testOnly <Spec>"`(见 [tool/test-specific-spec.sh](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/test-specific-spec.sh)),适合只想跑一个测试类时使用。

#### 4.4.4 代码实践

**实践目标**:用 `make container` 进容器,把 Makefile 的「Docker 包装」一层层剥开来看懂。

**操作步骤**:
1. 宿主机执行 `make container`。
2. 在容器里手动执行 `sbt run`,观察它是否在 `/workspace` 下生成 `top.sv`。
3. 退出容器后,在宿主机 `ls -la top.sv` 看时间戳是否更新。

**需要观察的现象 / 预期结果**:
- 容器内 `/workspace` 就是你的仓库根目录(因为挂载)。
- `sbt run` 结束后,`top.sv` 出现在仓库根目录。
- 因为 `Main` 无条件覆写,每次 `sbt run` 后 `top.sv` 的修改时间都会更新。

**说明**:无 Docker 环境时为「待本地验证」;可改为纯阅读:对照 4.4.3 的源码精读,口述 `make build` 会展开成哪条 `docker run` 命令。

#### 4.4.5 小练习与答案

**练习 1**:`make build` 为什么每次都会重新跑 `sbt run`,即使什么都没改?
> **参考答案**:因为目标 `top.v` 对应的文件名 `top.v` 从未被生成(`top.Main` 写的是 `top.sv`),make 始终认为该目标「需要重建」,于是无条件执行 recipe。这是一个「以文件名作为时间戳」的 Makefile 模型与实际产出文件名不一致导致的副作用。

**练习 2**:`make build-fpga` 和 `make build` 在「是否使用 Docker」上有什么区别?
> **参考答案**:`make build` 完全在 Docker 镜像里跑 `sbt run`;而 `make build-fpga` 虽然 `depends on top.v`(所以 RTL 仍由容器生成),但 Vivado 综合 itself 是在**宿主机**直接调用 `$(VIVADO)` 跑的——因为 Vivado 是商业 EDA 工具,不在开源镜像里。

## 5. 综合实践

本实践的目的是把本讲的四个模块串起来:你会真正跑一次构建,并发现那个「文件名陷阱」。

**任务**:执行 `make test` 观察测试运行 → 改 `build.sbt` 一处注释 → 重新 `make build` → 记录 `sbt run` 实际产出的文件名。

**操作步骤**:

1. **跑测试**:在宿主机执行 `make test`(若无 Docker,可改用已进容器的 `sbt test`)。
   - 观察它最终展开成 `docker run ... sbt test`。
   - 留意输出里的 `-oDT` 效果:测试用例按耗时从慢到快排列。
2. **改注释**:打开 `build.sbt`,把第 1 行的注释从
   ```
   // See README.md for license details.
   ```
   改成
   ```
   // See README.md for license details. (build env lecture)
   ```
   保存。
3. **重新构建**:执行 `make build`。
   - 观察日志:sbt 检测到 `build.sbt` 变化,会触发一次 reload。
   - 随后 `sbt run` 重新 elaborate。
4. **核对产出**:构建完成后,在仓库根目录执行 `ls -la top.sv top.v`。
   - 注意看哪个文件**存在**、哪个**不存在**。

**需要观察的现象 / 预期结果**:

- `make test` 能跑通(若依赖未缓存,首次会下载 Chisel 6.7.0 等,较慢)。
- `make build` 成功后,**`top.sv` 被更新**(修改时间为刚才);而 `top.v` **不存在**(会得到 `ls: cannot access 'top.v'` 之类的报错)。
- 由此得到本实践的关键结论:`sbt run` 实际产出的文件名是 **`top.sv`**,与 Makefile 目标名 `top.v` 不一致。这正是 `AGENTS.md` 专门提示、本讲反复强调的 gotcha。

**进阶思考(可选)**:既然 `top.v` 永远不存在,`make build` 每次必跑——这对开发迭代意味着什么?如果你想让「没改源码就不重新 elaborate」,可以怎么改 Makefile?(提示:把 `top.v:` 改名/改声明为 `.PHONY`,或者让 `top.Main` 写出 `top.v`。)

> 如果环境里没有 Docker,请把每一步的「预期结果」写成你的推断,并标注「待本地验证」——不要假装已经跑过命令。

## 6. 本讲小结

- chisel-npu 的构建链路是:**Chisel/Scala 源码 → sbt run → firtool → `top.sv`**,下游再接 verilator/SystemC 仿真或 Vivado 综合。
- `build.sbt` 定义了 Scala 2.13.12 + Chisel 6.7.0(主依赖)+ chiseltest 5.0.2(测试),并通过 chisel-plugin 编译插件、`-Ymacro-annotations` 等选项支撑 Chisel 宏;`name`/`organization` 留有模板占位符属正常现象。
- 整套工具链(firtool 1.62.1、verilator v5.036、SystemC 3.0.1、Chisel 6.7.0 本地发布)打包在 `fangruil/chisel-dev` 镜像里,这是「优先 Docker、不裸机 sbt」的根本原因。
- `Makefile` 本质是「把 sbt 命令包进 `docker run`」的薄封装;常用入口有 `image / container / test / build / build-sc / docs / clean`。
- **核心 gotcha**:Makefile 目标叫 `top.v`,但 `top.Main` 实际写出的是 `top.sv`,因此 `make build` 每次都会重新 `sbt run`,且 `build-sc` 需要先把文件改名为 `top.v`。
- FPGA 流程 `build-fpga` 依赖 `top.v`(生成 RTL)但 Vivado 综合本身在宿主机执行,是 U8 的入口。

## 7. 下一步学习建议

至此你已经能跑通构建、进出容器、理解工具链版本。建议接下来:

- 阅读 **u1-l3(源码目录结构与顶层入口)**:深入 `src/main/scala/` 的目录组织,理解 `top.Main` 之外还有哪些模块入口,以及 Chisel elaborate 的规模如何随 MMALU 参数变化。
- 如果你想立刻动手验证 RTL,可以先用 `make container` 进容器,尝试 `sbt run` 后用文本编辑器打开 `top.sv`,找一找里面的 `MMPE` 模块实例,感受一下「Scala 代码最终变成了什么样的 Verilog」。
- 暂时**不必**碰 `make build-fpga` / `make build-sc`,它们需要 Vivado 或额外的 SystemC 配置,留到 U8(FPGA 平台与驱动)再系统讲解。
