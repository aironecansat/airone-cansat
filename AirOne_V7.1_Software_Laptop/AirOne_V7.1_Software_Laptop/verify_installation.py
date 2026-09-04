#!/usr/bin/env python3
"""
AirOne V7.1 Installation Verification Script

Checks all dependencies, configuration, and system readiness.
Run this after installation to ensure everything is working correctly.
"""

import sys
import os
import subprocess
from pathlib import Path


def print_header(text):
    """Print section header."""
    print(f"\n{'='*70}")
    print(f"  {text}")
    print(f"{'='*70}")


def print_check(name, status, details=""):
    """Print check result."""
    symbol = "✓" if status else "✗"
    color = "\033[92m" if status else "\033[91m"
    reset = "\033[0m"
    print(f"{color}{symbol}{reset} {name:50} {details}")


def check_python_version():
    """Check Python version."""
    print_header("Python Environment")
    version = sys.version_info
    is_ok = version.major == 3 and version.minor >= 10
    print_check(
        "Python version",
        is_ok,
        f"{version.major}.{version.minor}.{version.micro}"
    )
    return is_ok


def check_required_packages():
    """Check required Python packages."""
    print_header("Required Python Packages")
    
    required = {
        "numpy": "1.24.3",
        "scipy": "1.11.4",
        "pandas": "2.1.4",
        "PyJWT": "2.10.1",
        "bcrypt": "4.2.1",
        "cryptography": "44.0.0",
        "flask": "3.1.0",
        "flask_cors": "5.0.0",
        "flask_limiter": "3.10.0",
        "marshmallow": "3.25.0",
        "pyserial": "3.5",
        "psutil": "6.1.1",
        "reedsolo": "2.1.0",
        "pytest": "9.1.1",
        "PyYAML": "6.0.2",
        "joblib": "1.5.3",
    }
    
    all_ok = True
    for package, expected_version in required.items():
        try:
            if package == "flask_cors":
                import flask_cors
                version = flask_cors.__version__
            elif package == "flask_limiter":
                import flask_limiter
                version = flask_limiter.__version__
            elif package == "PyJWT":
                import jwt
                version = jwt.__version__
            else:
                mod = __import__(package)
                version = mod.__version__
            
            print_check(f"{package}", True, f"v{version}")
        except ImportError:
            print_check(f"{package}", False, "NOT INSTALLED")
            all_ok = False
        except AttributeError:
            print_check(f"{package}", True, "installed (version unknown)")
    
    return all_ok


def check_optional_packages():
    """Check optional ML packages."""
    print_header("Optional ML Packages")
    
    optional = ["sklearn", "torch"]
    results = {}
    
    for package in optional:
        try:
            if package == "sklearn":
                import sklearn
                version = sklearn.__version__
                print_check("scikit-learn", True, f"v{version}")
                results[package] = True
            elif package == "torch":
                import torch
                version = torch.__version__
                print_check("torch (PyTorch)", True, f"v{version}")
                results[package] = True
        except ImportError:
            print_check(f"{package}", False, "not installed (optional)")
            results[package] = False
    
    return results


def check_gui_packages():
    """Check GUI packages."""
    print_header("GUI Packages (Optional)")
    
    gui_ok = True
    try:
        import PyQt5
        print_check("PyQt5", True, "installed")
    except ImportError:
        print_check("PyQt5", False, "not installed (GUI unavailable)")
        gui_ok = False
    
    try:
        import matplotlib
        print_check("matplotlib", True, f"v{matplotlib.__version__}")
    except ImportError:
        print_check("matplotlib", False, "not installed (plots unavailable)")
        gui_ok = False
    
    return gui_ok


def check_file_structure():
    """Check that all required directories and files exist."""
    print_header("File Structure")
    
    root = Path(__file__).parent
    
    required_dirs = [
        "src", "src/api", "src/core", "src/ml", "src/scientific",
        "src/security", "src/storage", "src/telemetry", "src/workers",
        "tests", "config", "docs", "data", "logs", "migrations"
    ]
    
    required_files = [
        "launcher.py", "requirements_v71.txt", "README.md",
        "config/default_config.yaml", "migrations/001_initial.sql"
    ]
    
    all_ok = True
    for dir_path in required_dirs:
        exists = (root / dir_path).is_dir()
        print_check(f"Directory: {dir_path}", exists)
        all_ok = all_ok and exists
    
    for file_path in required_files:
        exists = (root / file_path).is_file()
        print_check(f"File: {file_path}", exists)
        all_ok = all_ok and exists
    
    return all_ok


def check_environment():
    """Check environment variables."""
    print_header("Environment Configuration")
    
    jwt_secret = os.getenv("AIRONE_JWT_SECRET")
    if jwt_secret:
        is_secure = len(jwt_secret) >= 32 and jwt_secret != "ChangeThisSecret"
        print_check(
            "AIRONE_JWT_SECRET",
            is_secure,
            f"set ({len(jwt_secret)} chars)" if is_secure else "TOO WEAK"
        )
        return is_secure
    else:
        print_check("AIRONE_JWT_SECRET", False, "NOT SET (required)")
        return False


def check_tests():
    """Check if tests can be discovered."""
    print_header("Test Suite")
    
    try:
        result = subprocess.run(
            ["pytest", "--collect-only", "-q"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            # Count tests from output
            lines = result.stdout.strip().split("\n")
            test_count = sum(1 for line in lines if line.strip().startswith("tests/"))
            print_check("Test discovery", True, f"{test_count} tests found")
            return True
        else:
            print_check("Test discovery", False, "pytest collection failed")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print_check("Test discovery", False, "pytest not available")
        return False


def check_ml_detectors():
    """Check which ML detectors are available."""
    print_header("ML Detector Availability")
    
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from src.ml import available_detectors
        
        detectors = available_detectors()
        for name, info in detectors.items():
            available = info["available"]
            reason = info["reason"]
            print_check(name, available, reason)
        
        return any(d["available"] for d in detectors.values())
    except Exception as e:
        print_check("ML module import", False, str(e))
        return False


def main():
    """Run all verification checks."""
    print("\n" + "="*70)
    print("  AirOne V7.1 Installation Verification")
    print("="*70)
    
    results = {
        "Python version": check_python_version(),
        "Required packages": check_required_packages(),
        "File structure": check_file_structure(),
        "Environment config": check_environment(),
        "Tests": check_tests(),
    }
    
    # Optional checks (don't fail overall verification)
    optional_results = {
        "Optional ML packages": check_optional_packages(),
        "GUI packages": check_gui_packages(),
        "ML detectors": check_ml_detectors(),
    }
    
    # Summary
    print_header("Verification Summary")
    
    core_ok = all(results.values())
    
    if core_ok:
        print("\n\033[92m✓ CORE INSTALLATION VERIFIED\033[0m")
        print("\nThe system is ready to run:")
        print("  export AIRONE_JWT_SECRET=\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')\"")
        print("  python3 launcher.py --simulate")
    else:
        print("\n\033[91m✗ CORE INSTALLATION INCOMPLETE\033[0m")
        print("\nPlease fix the following issues:")
        for check, status in results.items():
            if not status:
                print(f"  - {check}")
        print("\nThen run this script again.")
        return 1
    
    # Optional warnings
    if not optional_results["Optional ML packages"]["sklearn"]:
        print("\n\033[93m⚠\033[0m  scikit-learn not installed (IsolationForest, GMM unavailable)")
        print("    Install: pip install scikit-learn")
    
    if not optional_results["Optional ML packages"]["torch"]:
        print("\n\033[93m⚠\033[0m  PyTorch not installed (LSTM forecaster unavailable)")
        print("    Install: pip install torch")
    
    if not optional_results["GUI packages"]:
        print("\n\033[93m⚠\033[0m  PyQt5/matplotlib not installed (GUI unavailable)")
        print("    Install: pip install PyQt5 matplotlib")
    
    print("\n" + "="*70)
    print("  Ready for deployment!")
    print("="*70 + "\n")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
