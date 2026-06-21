function redmine-shift --description 'Составить смену: мои задачи (или указанных user_id) за сегодня+вчера → буфер'
    # Тонкая обёртка над ~/.local/bin/redmine-shift (тот же скрипт зовёт quickshell-меню Super+B).
    #   redmine-shift            — только я
    #   redmine-shift 707 723    — объединить смену по нескольким user_id
    $HOME/.local/bin/redmine-shift $argv
end
