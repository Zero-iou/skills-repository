hs.hotkey.bind({"cmd", "shift"}, "E", function()
    hs.task.new("/bin/zsh", nil, {"-c", os.getenv("HOME") .. "/.hammerspoon/record_e.sh"}):start()
end)

hs.hotkey.bind({"cmd", "shift"}, "D", function()
    hs.task.new("/bin/zsh", nil, {"-c", os.getenv("HOME") .. "/.hammerspoon/record_d.sh"}):start()
end)
