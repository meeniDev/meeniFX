#version 330
uniform sampler2D source;
out vec4 result;
void main() {
    vec4 pixel = texelFetch(source, ivec2(gl_FragCoord.xy), 0).bgra;
    vec3 linear = mix(pow((pixel.rgb + .055) / 1.055, vec3(2.4)),
                      pixel.rgb / 12.92, lessThanEqual(pixel.rgb, vec3(.04045)));
    result = vec4(linear * pixel.a, pixel.a);
}
