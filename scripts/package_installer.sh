 #!/usr/bin/env bash
  while IFS= read -r pkg; do
    # skip empty lines and comments
    [[ -z "$pkg" || "$pkg" =~ ^# ]] && continue
    echo "Installing: $pkg"
    pip install "$pkg" || echo "FAILED: $pkg" 1>&2
  done < requirements.txt
