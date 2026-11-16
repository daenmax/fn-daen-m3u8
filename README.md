# fn-daen-m3u8
Daen的m3u8下载器

飞牛Nas原生应用：Daen的m3u8下载器，用来多线程下载m3u8到NAS，支持各大资源网

# 使用

1. 应用安装好之后，应用设置-访问权限，添加一下

2. 打开资源搜索地址
```
非凡资源网 http://www.ffzy.tv/

量子资源网 http://www.lzizy.com/

无尽资源网 https://www.wujinzy.cc/
```

3. 找到后缀带m3u8的

4. 点击`复制名称$链接`或者`复制名称$链接$后缀`

5. 粘贴到NAS应用里，填写好文件夹名称，点击`开始下载`


# 构建
## 1.官网下载fnpack
https://developer.fnnas.com/docs/cli/fnpack/

Windows的下载下来是fnpack-1.2.3-windows-amd64

把文件名改为：fnpack.exe
## 2.打包项目
切换到当前项目的上层目录，打开cmd
```
fnpack build fn-daen-m3u8
```