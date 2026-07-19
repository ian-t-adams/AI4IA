using System.Security.Cryptography;
using System.Text;

namespace SimpleL7Proxy.Config;

/// <summary>
/// Compares opaque high-entropy secrets (API keys, subscription keys) safely.
/// Unlike identifiers or header names, these values are not case-insensitive
/// by convention, and must not be compared with a short-circuiting method
/// that can leak timing information about how many leading bytes matched.
/// </summary>
public static class SecretComparer
{
    /// <summary>
    /// Returns true only if both values are non-null and byte-for-byte
    /// identical (case-sensitive). Comparison time does not depend on where
    /// the first mismatching byte occurs, avoiding a timing side-channel.
    /// </summary>
    public static bool FixedTimeEquals(string? left, string? right)
    {
        if (left is null || right is null)
        {
            return false;
        }

        var leftBytes = Encoding.UTF8.GetBytes(left);
        var rightBytes = Encoding.UTF8.GetBytes(right);

        // CryptographicOperations.FixedTimeEquals already short-circuits on a
        // length mismatch without inspecting content, so no separate length
        // check is needed here.
        return CryptographicOperations.FixedTimeEquals(leftBytes, rightBytes);
    }
}
